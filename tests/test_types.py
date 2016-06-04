import arrow
import decimal
import pytest
import uuid
from bloop import types


def symmetric_test(typedef, *pairs):
    """ Test any number of load/dump pairs for a symmetric `Type` instance """
    for (loaded, dumped) in pairs:
        assert typedef.dynamo_load(dumped) == loaded
        assert typedef.dynamo_dump(loaded) == dumped


def test_load_dump_best_effort():
    """ can_* are not called when trying to load values """

    class MyType(types.Type):
        backing_type = "FOO"
        python_type = float

    typedef = MyType()
    assert typedef._load({"NOT_FOO": "not_a_float"}) == "not_a_float"
    assert typedef._dump("not_a_float") == {"FOO": "not_a_float"}


def test_string():
    typedef = types.String()
    symmetric_test(typedef, ("foo", "foo"))


def test_uuid():
    typedef = types.UUID()
    uuid_obj = uuid.uuid4()
    uuid_str = str(uuid_obj)
    symmetric_test(typedef, (uuid_obj, uuid_str))


def test_datetime():
    typedef = types.DateTime()

    tz = "Europe/Paris"
    now = arrow.now()

    # Not a symmetric type
    assert typedef.dynamo_load(now.isoformat()) == now
    assert typedef.dynamo_dump(now) == now.to("utc").isoformat()

    assert typedef.dynamo_load(now.to(tz).isoformat()) == now
    assert typedef.dynamo_dump(now.to(tz)) == now.to("utc").isoformat()

    # Should load values in the given timezone.
    # Because arrow objects compare equal regardless of timezone, we
    # isoformat each to compare the rendered strings (which preserve tz).
    local_typedef = types.DateTime(timezone=tz)
    loaded_as_string = local_typedef.dynamo_load(now.isoformat()).isoformat()
    now_with_tz_as_string = now.to(tz).isoformat()
    assert loaded_as_string == now_with_tz_as_string


def test_float():
    typedef = types.Float()
    d = decimal.Decimal

    errors = [
        (d(4/3), decimal.Inexact),
        (d(10) ** 900, decimal.Overflow),
        (d(0.9) ** 9000, decimal.Underflow),
        ("Infinity", TypeError),
        (d("NaN"), TypeError)
    ]
    for value, raises in errors:
        with pytest.raises(raises):
            typedef.dynamo_dump(value)

    symmetric_test(typedef,
                   (1.5, "1.5"),
                   (d(4)/d(3), "1.333333333333333333333333333"))


def test_integer():
    """
    Integer is a thin wrapper over Float that exposes non-decimal objects
    """
    typedef = types.Integer()

    symmetric_test(typedef, (4, "4"))

    assert typedef.dynamo_dump(4.5) == "4"
    assert typedef.dynamo_load("4") == 4

    # Corrupted data is truncated
    assert typedef.dynamo_load("4.5") == 4


def test_binary():
    typedef = types.Binary()
    symmetric_test(typedef, (b"123", "MTIz"), (bytes(1), "AA=="))


def test_sets():

    # Helper since sets are unordered, but dump must return an ordered list
    def check(dumped, expected):
        assert set(dumped) == expected

    tests = [
        (types.Set(types.String),
         set(["Hello", "World"]),
         set(["Hello", "World"])),
        (types.Set(types.Float),
         set([4.5, 3]),
         set(["4.5", "3"])),
        (types.Set(types.Integer),
         set([0, -1, 1]),
         set(["0", "-1", "1"])),
        (types.Set(types.Binary),
         set([b"123", b"456"]),
         set(["MTIz", "NDU2"]))
    ]

    for (typedef, loaded, expected) in tests:
        dumped = typedef.dynamo_dump(loaded)
        check(dumped, expected)
        assert typedef.dynamo_load(expected) == loaded


def test_set_type_instance():
    """ Set can take an instance of a Type as well as a Type subclass """
    type_instance = types.String()
    instance_set = types.Set(type_instance)
    assert instance_set.typedef is type_instance

    type_subclass = types.String
    subclass_set = types.Set(type_subclass)
    assert isinstance(subclass_set.typedef, type_subclass)


def test_bool():
    """ Boolean will never store/load as empty - bool(None) is False """
    typedef = types.Boolean()

    truthy = [1, True, object(), bool, "str"]
    falsy = [False, None, 0, set(), ""]

    for value in truthy:
        assert typedef.dynamo_dump(value) is True
        assert typedef.dynamo_load(value) is True

    for value in falsy:
        assert typedef.dynamo_dump(value) is False
        assert typedef.dynamo_load(value) is False


def test_list():
    typedef = types.List(types.UUID)
    loaded = [uuid.uuid4() for _ in range(5)]
    expected = [{"S": str(id)} for id in loaded]

    dumped = typedef.dynamo_dump(loaded)
    assert dumped == expected
    assert typedef.dynamo_load(dumped) == loaded


def test_required_subtypes():
    """Typed containers require an inner type"""
    for typeclass in [types.List, types.Set, types.TypedMap]:
        with pytest.raises(TypeError):
            typeclass()


def test_load_dump_none():
    """ Loading or dumping None returns None """
    typedef = types.String()
    assert typedef._dump(None) == {"S": None}
    assert typedef._load({"S": None}) is None


def test_map_dump(document_type):
    """ Map handles nested maps and custom types """
    uid = uuid.uuid4()
    now = arrow.now().to('utc')
    loaded = {
        'Rating': 0.5,
        'Stock': 3,
        'Description': {
            'Heading': "Head text",
            'Body': "Body text",
            'Specifications': None
        },
        'Id': uid,
        'Updated': now
    }
    expected = {
        'Rating': {'N': '0.5'},
        'Stock': {'N': '3'},
        'Description': {
            'M': {
                'Heading': {'S': 'Head text'},
                'Body': {'S': 'Body text'}}},
        'Id': {'S': str(uid)},
        'Updated': {'S': now.isoformat()}
    }
    dumped = document_type.dynamo_dump(loaded)
    assert dumped == expected


def test_map_load(document_type):
    """ Map handles nested maps and custom types """
    uid = uuid.uuid4()
    dumped = {
        'Rating': {'N': '0.5'},
        'Stock': {'N': '3'},
        'Description': {
            'M': {
                'Heading': {'S': 'Head text'},
                'Body': {'S': 'Body text'}}},
        'Id': {'S': str(uid)}
    }
    expected = {
        'Rating': 0.5,
        'Stock': 3,
        'Description': {
            'Heading': "Head text",
            'Body': "Body text",
            'Specifications': None
        },
        'Id': uid,
        'Updated': None
    }
    loaded = document_type.dynamo_load(dumped)
    assert loaded == expected


def test_typedmap():
    """ TypedMap handles arbitary keys and values """
    typedef = types.TypedMap(types.DateTime)

    now = arrow.now().to('utc')
    later = now.replace(seconds=30)
    loaded = {
        'now': now,
        'later': later
    }
    dumped = {
        'now': {'S': now.isoformat()},
        'later': {'S': later.isoformat()}
    }
    assert typedef.dynamo_dump(loaded) == dumped
    assert typedef.dynamo_load(dumped) == loaded
