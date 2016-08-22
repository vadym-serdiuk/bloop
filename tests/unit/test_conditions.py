import operator
import uuid

import pytest
from bloop.conditions import (
    And,
    AttributeExists,
    BeginsWith,
    Between,
    Comparison,
    Condition,
    Contains,
    In,
    Not,
    Or,
    iter_columns,
)
from bloop.exceptions import InvalidComparisonOperator
from bloop.expressions import ConditionRenderer, render
from bloop.models import BaseModel, Column
from bloop.types import UUID, Integer, TypedMap

from ..helpers.models import (
    ComplexModel,
    Document,
    DocumentType,
    User,
    conditions,
)


def test_duplicate_name_refs(engine):
    """ name refs are re-used for the same name """
    renderer = ConditionRenderer(engine)
    assert renderer.name_ref(User.age) == renderer.name_ref(User.age) == "#n0"


def test_no_refs(engine):
    """
    when name/value refs are missing, ExpressionAttributeNames/Values
    aren't populated """
    condition = And()
    expected = {}
    assert render(engine, condition=condition) == expected


@pytest.mark.parametrize("op", [operator.and_, operator.or_])
@pytest.mark.parametrize("empty_cls", [Condition, Or, And])
def test_basic_simplification(op, empty_cls):
    condition = Comparison(User.name, "==", "foo")
    same = op(condition, empty_cls())
    assert same is condition


@pytest.mark.parametrize("cls", [Condition, Or, And])
def test_negate_empty_conditions(cls):
    empty = cls()
    assert ~empty is empty


@pytest.mark.parametrize("cls, op", [(And, operator.and_), (Or, operator.or_)])
@pytest.mark.parametrize("empty_cls", [Condition, Or, And])
def test_shortcut_multi_appends(cls, op, empty_cls):
    # And() & None -> same And
    # Or() & None -> same Or
    obj = cls()
    same = op(obj, empty_cls())
    assert same is obj


def test_double_negate():
    condition = Comparison(User.name, "==", "foo")
    assert ~~condition is condition


def test_condition_ops():
    age, name = (User.age >= 3), (User.name == "foo")

    assert age & name == And(age, name)
    assert age | name == Or(age, name)
    assert ~age == Not(age)


def test_condition_len():
    age, name = (User.age >= 3), (User.name == "foo")
    and_condition = age & name
    or_condition = And(age, name, age)
    not_condition = ~age

    assert len(or_condition) == 3
    assert len(and_condition) == 2
    assert len(age) == len(name) == len(not_condition) == 1


def test_multi_shortcut():
    """ And or Or with single conditions render as their sole condition """
    age = User.age >= 3
    condition = And(age)
    assert condition.conditions == [age]

    condition = Or(age)
    assert condition.conditions == [age]


def test_multi_chains_flatten():
    """
    ((condition & condition) & condition) flattens the AND into one condition
    """
    age = User.age >= 3
    name = User.name == "foo"
    email = User.email != "bar"

    # Flatten left -> right:  (a & b) & c -> a & b & c
    and_condition = Condition()
    or_condition = Condition()
    for c in [age, name, email]:
        and_condition &= c
        or_condition |= c
    assert and_condition == And(age, name, email)
    assert or_condition == Or(age, name, email)

    # Flatten right -> left:  a & (b & c) -> a & b & c
    and_condition = Condition()
    or_condition = Condition()
    for c in [age, name, email]:
        and_condition = c & and_condition
        or_condition = c | or_condition
    assert and_condition == And(email, name, age)
    assert or_condition == Or(email, name, age)


def test_not(engine):
    age = ~(User.age >= 3)
    condition = And(age)
    expected = {
        "ConditionExpression": "(NOT (#n0 >= :v1))",
        "ExpressionAttributeNames": {"#n0": "age"},
        "ExpressionAttributeValues": {":v1": {"N": "3"}}}
    assert render(engine, condition=condition) == expected


def test_invalid_comparator():
    with pytest.raises(InvalidComparisonOperator):
        Comparison(User.age, "not-a-comparator", 5)


def test_attribute_exists(engine):
    condition = User.age.is_not(None)
    expected = {
        "ConditionExpression": "(attribute_exists(#n0))",
        "ExpressionAttributeNames": {"#n0": "age"}}
    assert render(engine, condition=condition) == expected


def test_attribute_not_exists(engine):
    condition = User.age.is_(None)
    expected = {
        "ConditionExpression": "(attribute_not_exists(#n0))",
        "ExpressionAttributeNames": {"#n0": "age"}}
    assert render(engine, condition=condition) == expected


def test_begins_with(engine):
    condition = User.name.begins_with("foo")
    expected = {
        "ConditionExpression": "(begins_with(#n0, :v1))",
        "ExpressionAttributeNames": {"#n0": "name"},
        "ExpressionAttributeValues": {":v1": {"S": "foo"}}}
    assert render(engine, condition=condition) == expected


def test_contains(engine):
    condition = User.name.contains("foo")
    expected = {
        "ConditionExpression": "(contains(#n0, :v1))",
        "ExpressionAttributeNames": {"#n0": "name"},
        "ExpressionAttributeValues": {":v1": {"S": "foo"}}}
    assert render(engine, condition=condition) == expected


def test_between(engine):
    condition = User.name.between("bar", "foo")
    expected = {
        "ConditionExpression": "(#n0 BETWEEN :v1 AND :v2)",
        "ExpressionAttributeNames": {"#n0": "name"},
        "ExpressionAttributeValues": {":v1": {"S": "bar"}, ":v2": {"S": "foo"}}}
    assert render(engine, condition=condition) == expected


def test_in(engine):
    condition = User.name.in_(["bar", "foo"])
    expected = {
        "ConditionExpression": "(#n0 IN (:v1, :v2))",
        "ExpressionAttributeNames": {"#n0": "name"},
        "ExpressionAttributeValues": {":v1": {"S": "bar"}, ":v2": {"S": "foo"}}}
    assert render(engine, condition=condition) == expected


def test_base_condition(engine):
    """ (Condition() OP condition) is condition """
    base = Condition()
    other = User.email == "foo"

    assert (base & other) is other
    assert (base | other) is other
    assert (~base) is base
    assert len(base) == 0

    assert base.render(object()) is None

    assert not render(engine, condition=base)


def test_render_path(engine):
    """ A path should be rendered as #column.#field.#field """
    renderer = ConditionRenderer(engine)
    path = "foo bar baz".split()
    renderer.name_ref(User.email, path=path)
    expected = {'ExpressionAttributeNames': {'#n0': 'email', '#n3': 'baz', '#n2': 'bar', '#n1': 'foo'}}
    assert renderer.rendered == expected


def test_path_comparator(engine):
    """ Render paths for operations, comparisons, and multi-conditions """
    rating = Document.data["Rating"] > 0.5
    no_body = Document.data["Description"]["Body"].is_(None)
    stock = Document.data["Stock"].in_([1, 2, 3])
    condition = (rating & no_body) | stock

    expected = {
        'ConditionExpression': (
            '(((#n0.#n1 > :v2) AND (attribute_not_exists(#n0.#n3.#n4))) OR (#n0.#n5 IN (:v6, :v7, :v8)))'),
        'ExpressionAttributeValues': {
            ':v2': {'N': '0.5'}, ':v6': {'N': '1'}, ':v7': {'N': '2'}, ':v8': {'N': '3'}},
        'ExpressionAttributeNames': {
            '#n0': 'data', '#n1': 'Rating', '#n3': 'Description', '#n4': 'Body', '#n5': 'Stock'}}
    assert render(engine, condition=condition) == expected


def test_typedmap_path_comparator(engine):
    """ TypedMap should defer to the value typedef for conditions """
    class Model(BaseModel):
        id = Column(Integer, hash_key=True)
        data = Column(TypedMap(UUID))
    engine.bind(base=Model)

    uid = uuid.uuid4()
    condition = Model.data['foo'].is_(uid)

    expected = {
        'ConditionExpression': '(#n0.#n1 = :v2)',
        'ExpressionAttributeNames': {'#n0': 'data', '#n1': 'foo'},
        'ExpressionAttributeValues': {':v2': {'S': str(uid)}}}
    assert render(engine, condition=condition) == expected


def test_name_ref_with_path(engine):
    """ Columns with custom names with literal periods render correctly """
    class Model(BaseModel):
        id = Column(Integer, hash_key=True, name='this.is.id')
        data = Column(DocumentType)
    engine.bind(base=Model)

    no_id = Model.id.is_(None)
    path_condition = Model.data["Rating"] >= 2
    condition = no_id & path_condition

    expected = {
        'ConditionExpression': '((attribute_not_exists(#n0)) AND (#n1.#n2 >= :v3))',
        'ExpressionAttributeNames': {'#n0': 'this.is.id', '#n1': 'data', '#n2': 'Rating'},
        'ExpressionAttributeValues': {':v3': {'N': '2'}}}
    assert render(engine, condition=condition) == expected


def test_list_path(engine):
    """ render list indexes correctly """
    condition = Document.numbers[1] >= 3
    expected = {
        'ConditionExpression': '(#n0[1] >= :v1)',
        'ExpressionAttributeNames': {'#n0': 'numbers'},
        'ExpressionAttributeValues': {':v1': {'N': '3'}}}
    assert render(engine, condition=condition) == expected


# If we parametrize conditions x conditions, the test count explode into a
# useless number, so we only parametrize one. This should still make isolating
# failures easier, from O(len(conditions*conditions)) when neither
# is parametrized to O(len(conditions))
@pytest.mark.parametrize("condition", conditions, ids=str)
def test_equality(condition):
    for other in conditions:
        if condition is other:
            assert condition == other
        else:
            assert condition != other


def test_complex_iter_columns():
    """Includes cycles, empty conditions, Not, MultiConditions"""

    first_comp = ComplexModel.name == "foo"
    second_comp = ComplexModel.date == "bar"
    third_comp = ComplexModel.email == "baz"

    negate = Not(third_comp)
    empty = Condition()

    both = first_comp & second_comp
    either = Or(negate, empty)

    # cycle = (
    #   (1 & 2) &
    #   (~ | _) &
    #   cycle
    # )
    cycle = And(both, either)
    cycle.conditions.append(cycle)

    expected = {
        ComplexModel.name,
        ComplexModel.date,
        ComplexModel.email
    }
    assert set(iter_columns(cycle)) == expected


def test_condition_repr():
    assert repr(Condition()) == "<empty condition>"


def test_and_repr():
    empty = Condition()

    assert repr(And(empty)) == "({} &)".format(repr(empty))
    assert repr(And(empty, empty)) == "({0} & {0})".format(repr(empty))


def test_or_repr():
    empty = Condition()

    assert repr(Or(empty)) == "({} |)".format(repr(empty))
    assert repr(Or(empty, empty)) == "({0} | {0})".format(repr(empty))


def test_not_repr():
    empty = Condition()

    assert repr(Not(empty)) == "(~{})".format(repr(empty))


def test_comparison_repr():
    operators = ["==", "!=", "<", ">", "<=", ">="]
    value = "foo"
    column = User.age

    for op in operators:
        assert repr(Comparison(column, op, value)) == "(User.age {} 'foo')".format(op)


def test_attribute_exists_repr():
    column = User.age

    assert repr(AttributeExists(column, False)) == "(exists User.age)"
    assert repr(AttributeExists(column, True)) == "(not_exists User.age)"


def test_begins_with_repr():
    value = "foo"
    column = User.age

    assert repr(BeginsWith(column, value)) == "(User.age begins with 'foo')"


def test_contains_repr():
    value = "foo"
    column = User.age

    assert repr(Contains(column, value)) == "(User.age contains 'foo')"


def test_between_repr():
    lower = "3"
    higher = 3
    column = User.age

    assert repr(Between(column, lower, higher)) == "(User.age between ['3', 3])"


def test_in_repr():
    values = ["foo", 3]
    column = User.age

    assert repr(In(column, values)) == "(User.age in ['foo', 3])"


def test_path_repr():
    column = User.age
    path = ["foo", 3, "bar", "baz", 2, 1]

    condition = Comparison(column, ">", 0, path=path)

    assert repr(condition) == "(User.age.foo[3].bar.baz[2][1] > 0)"
