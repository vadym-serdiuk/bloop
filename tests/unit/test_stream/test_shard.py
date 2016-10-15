import pytest
import random
import string
# import arrow
from bloop.exceptions import ShardIteratorExpired
from bloop.session import SessionWrapper
from bloop.stream.shard import CALLS_TO_REACH_HEAD, Shard, last_iterator, reformat_record, unpack_shards
from typing import Dict, List, Union, Any
from unittest.mock import call


@pytest.fixture
def shard(session):
    return Shard(stream_arn="stream_arn", shard_id="shard_id", session=session)


def random_str(prefix="", length=8):
    return prefix + "".join(random.choice(string.ascii_lowercase) for _ in range(length))


def build_shards(n: int, shape: Dict[int, Union[int, List[int]]],
                 session: SessionWrapper=None, stream_arn=None) -> List[Shard]:
    """Shape describes the parent/child relationships.

    a -> b -> c -> d
           -> e -> f

    is expressed as:

    build_shards(session, 6, {0: 1, 1: [2, 3], 2: 4, 3: 5})
    """
    shard_id = lambda i: random_str("shard-id-{}-".format(i), 4)
    shards = [
        Shard(stream_arn=stream_arn, shard_id=shard_id(i), session=session)
        for i in range(n)
    ]
    for shard_index, child_indexes in shape.items():
        if isinstance(child_indexes, int):
            shards[shard_index].children.append(shards[child_indexes])
            shards[child_indexes].parent = shards[shard_index]
        else:
            for child_index in child_indexes:
                shards[shard_index].children.append(shards[child_index])
                shards[child_index].parent = shards[shard_index]

    return shards


def stream_description(n: int, shape: Dict[int, Union[int, List[int]]], stream_arn=None) -> Dict[str, Any]:
    """Build a DescribeStream response with the given number of shards"""
    shard_ids = [random_str("shard_id-{}-".format(i), 4) for i in range(n)]
    template = {
        "SequenceNumberRange": {
            "EndingSequenceNumber": "820400000000000001192334",
            "StartingSequenceNumber": "820400000000000001192334"
        }
    }
    shards = [{**template, "ShardId": shard_id} for shard_id in shard_ids]

    for shard_index, child_indexes in shape.items():
        if isinstance(child_indexes, int):
            shards[child_indexes]["ParentShardId"] = shard_ids[shard_index]
        else:
            for child_index in child_indexes:
                shards[child_index]["ParentShardId"] = shard_ids[shard_index]
    return {
        "Shards": shards,
        "StreamArn": stream_arn
    }


def record_with(key=False, new=False, old=False, sequence_number=None):
    template = {
        "awsRegion": "us-west-2",
        "dynamodb": {
            "ApproximateCreationDateTime": 1.46480527E9,
            "SequenceNumber": sequence_number if sequence_number is not None else "400000000000000499660",
            "SizeBytes": 41,
            "StreamViewType": "KEYS_ONLY",

            "Keys": {
                "ForumName": {"S": "DynamoDB"},
                "Subject": {"S": "DynamoDB Thread 1"}},
            "NewImage": {
                "ForumName": {"S": "DynamoDB"},
                "Subject": {"S": "DynamoDB Thread 1"}},
            "OldImage": {
                "ForumName": {"S": "DynamoDB"},
                "Subject": {"S": "DynamoDB Thread 1"}}
        },
        "eventID": "4b25bd0da9a181a155114127e4837252",
        "eventName": "MODIFY",
        "eventSource": "aws:dynamodb",
        "eventVersion": "1.0"
    }
    if not key:
        del template["dynamodb"]["Keys"]
    if not new:
        del template["dynamodb"]["NewImage"]
    if not old:
        del template["dynamodb"]["OldImage"]
    return template


def build_get_records_responses(*chain):
    """Return an iterable of responses for session.get_stream_records calls.

    Chain is the number of results to include in each page.
    For example: [0, 2, 1] expands into (0 results, proceed) -> (2 results, proceed) -> (1 result, stop).
    Very similar to the build_responses helper in test_search.py
    """
    sequence_number = 0
    responses = []
    for i, count in enumerate(chain):
        responses.append({
            "Records": [record_with(key=True, sequence_number=sequence_number + offset) for offset in range(count)],
            "NextShardIterator": "continue-from-response-{}".format(i)
        })
        sequence_number += count
    # Last response doesn't point to a new iterator
    del responses[-1]["NextShardIterator"]

    return responses


def expected_get_calls(chain):
    """Returns the expected number of get_records calls including catch-up logic, to exhaust the chain.

    For example, [3, 0, 1, 0, 0, 0, 1] will take 3 calls:
        - call 1 stops after page 1 (3 records) (no empty responses)
        - call 2 stops after page 3 (1 record)  (1 empty response) <- catch-up applied
        - call 3 stops after page 7 (1 record)  (4 empty responses) <- catch-up applied
    Or, [0, 0, 0, 1, 0, 0, 0, 1] will take 4 calls:
        - call 1 stops after page 4 (1 record)  (3 empty responses)
        - call 2 stops after page 6 (0 record)  (5 empty response) <- stopped due to catch-up limit
        - call 3 stops after page 7 (0 record)  (6 empty responses) <- only 1 try since catch-up reached
        - call 4 stops after page 8 (1 record)  (6 empty responses)
    """
    count = 0

    # 0) Every non-empty page is another Shard.get_records() call
    empty_pages = chain.count(0)
    non_empty = len(chain) - empty_pages

    count += non_empty

    # 1) If calls_to_reach_head is 5, every empty page after the 4th
    #    is another Shard.get_records() call
    has_free_empty_pages = True
    if empty_pages >= CALLS_TO_REACH_HEAD:
        has_free_empty_pages = False
        count += empty_pages - (CALLS_TO_REACH_HEAD - 1)

    # 2) If the last page is empty and we're out of free empty pages,
    #    then the last page has already been counted.
    #
    #    But if we still have free pages, the last empty page needs to be
    #    explicitly counted, since we had to call Shard.get_records()
    #    to learn that there were no more pages, and it wasn't really free.
    if chain[-1] == 0 and has_free_empty_pages:
        count += 1

    return count


@pytest.mark.parametrize("expected, kwargs", [
    ("<Shard[exhausted, id='shard-id']>", {"iterator_id": last_iterator}),
    ("<Shard[at_seq='sequence', id='shard-id']>",
     {"sequence_number": "sequence", "iterator_type": "at_sequence"}),
    ("<Shard[after_seq='sequence', id='shard-id']>",
     {"sequence_number": "sequence", "iterator_type": "after_sequence"}),
    ("<Shard[latest, id='shard-id']>", {"iterator_type": "latest"}),
    ("<Shard[trim_horizon, id='shard-id']>", {"iterator_type": "trim_horizon"}),
    ("<Shard[id='shard-id']>", {}),
])
def test_repr(expected, kwargs):
    shard = Shard(stream_arn="stream-arn", shard_id="shard-id", **kwargs)
    assert repr(shard) == expected


@pytest.mark.parametrize("iterator_type", ["latest", "trim_horizon"])
def test_next_raises_expired_without_sequence(iterator_type, shard, session):
    """If the iterator expires and didn't have a sequence_number, there's no way to safely re-create it."""
    shard.sequence_number = None
    shard.iterator_type = iterator_type
    shard.iterator_id = "iterator-id"

    exception = session.get_stream_records.side_effect = ShardIteratorExpired()

    with pytest.raises(ShardIteratorExpired) as excinfo:
        next(shard)

    # Exception is raised directly
    assert excinfo.value is exception
    session.get_stream_records.assert_called_once_with("iterator-id")
    # Didn't try to get a new iterator
    session.get_shard_iterator.assert_not_called()


@pytest.mark.parametrize("iterator_type", ["at_sequence", "after_sequence"])
def test_next_refreshes_expired_with_sequence(iterator_type, shard, session):
    """If the iterator expires and has a sequence_number, it will try to refresh."""
    shard.stream_arn = "stream-arn"
    shard.shard_id = "shard-id"
    shard.sequence_number = "sequence-number"
    shard.iterator_type = iterator_type
    shard.iterator_id = "expired-iterator-id"

    # Single response with 4 records
    response = build_get_records_responses(4)[0]
    session.get_stream_records.side_effect = [ShardIteratorExpired(), response]
    session.get_shard_iterator.return_value = "new-iterator-id"

    records = next(shard)
    # Don't need to deep validate here; that's covered by get_records and reformat_record tests.
    assert len(records) == len(response["Records"])

    # Only jumped once
    session.get_shard_iterator.assert_called_once_with(
        stream_arn=shard.stream_arn, shard_id=shard.shard_id,
        iterator_type=iterator_type, sequence_number="sequence-number"
    )
    # First call raised Expired, second call returned records
    session.get_stream_records.assert_has_calls([
        call("expired-iterator-id"),
        call("new-iterator-id")
    ])


@pytest.mark.parametrize("attr", [
    "stream_arn", "shard_id", "iterator_id", "iterator_type",
    "sequence_number", "parent"])
def test_eq_not_set_or_different(attr):
    parent = Shard(stream_arn="parent-arn", shard_id="parent-id")
    children = [Shard(stream_arn="child-arn", shard_id="child-id") for _ in range(2)]
    kwargs = {
        "stream_arn": "stream-arn",
        "shard_id": "shard-id",
        "iterator_id": "iterator-id",
        "iterator_type": "iterator-type",
        "sequence_number": "sequence-number",
        "parent": parent
    }
    shard = Shard(**kwargs)
    other = Shard(**kwargs)
    # Initially equal
    assert shard == other
    assert other == shard

    shard.children.extend(children)
    assert not shard == other
    assert not other == shard

    # Compare equal regardless of order
    other.children.extend(children[::-1])
    assert shard == other
    assert other == shard

    setattr(other, attr, random_str())
    assert not shard == other
    assert not other == shard


def test_exhausted(shard):
    assert shard.iterator_id is None
    assert not shard.exhausted

    shard.iterator_id = last_iterator
    assert shard.exhausted

    shard.iterator_id = None
    assert not shard.exhausted


def test_walk_tree():
    shards = build_shards(10, {
        0: 1,
        1: [2, 3],
        2: [4, 5, 6],
        3: [7, 8],
        4: 9
    })

    shard_ids = [shard.shard_id for shard in shards]

    root = shards[0]

    walked_shard_ids = [shard.shard_id for shard in root.walk_tree()]
    assert set(shard_ids) == set(walked_shard_ids)


def test_jump_to(shard, session):
    shard.empty_responses = 3
    shard.shard_id = "shard-id"
    shard.iterator_id = "iterator-id"
    shard.iterator_type = "iterator-type"
    shard.sequence_number = "sequence-number"
    shard.stream_arn = "stream-arn"

    session.get_shard_iterator.return_value = "new-shard-id"

    shard.jump_to(iterator_type="latest", sequence_number="different-sequence-number")

    assert shard.iterator_id == "new-shard-id"
    assert shard.iterator_type == "latest"
    assert shard.sequence_number == "different-sequence-number"
    assert shard.empty_responses == 0

    session.get_shard_iterator.assert_called_once_with(
        stream_arn="stream-arn",
        shard_id="shard-id",
        iterator_type="latest",
        sequence_number="different-sequence-number")


def test_load_existing_children(session):
    shards = build_shards(3, {0: [1, 2]}, session=session)
    root = shards[0]

    children = root.children[:]
    root.load_children()
    assert root.children == children
    session.describe_stream.assert_not_called()


def test_load_children(session):
    description = stream_description(5, {0: 1, 1: [2, 3]}, stream_arn="stream-arn")
    session.describe_stream.return_value = description

    # First shard in the description is unrelated to the root
    root = Shard(
        stream_arn="stream-arn",
        shard_id=description["Shards"][0]["ShardId"],
        session=session)
    assert not root.children

    # 0 -> 1 -> 2
    #        -> 3
    # 4
    child_id = description["Shards"][1]["ShardId"]
    first_grandchild_id = description["Shards"][2]["ShardId"]
    second_grandchild_id = description["Shards"][3]["ShardId"]

    # Loading shouldn't rely on implicit ordering
    random.shuffle(description["Shards"])
    root.load_children()

    assert set(s.shard_id for s in root.children) == {child_id}
    assert root.children[0].shard_id == child_id
    grandchild_ids = [s.shard_id for s in root.children[0].children]
    assert set(grandchild_ids) == {first_grandchild_id, second_grandchild_id}

    session.describe_stream.assert_called_once_with(stream_arn="stream-arn", first_shard=root.shard_id)


def test_get_records_exhausted(shard, session):
    shard.iterator_id = last_iterator

    records = shard.get_records()
    assert not records
    session.get_stream_records.assert_not_called()


def test_get_records_after_head(shard, session):
    """Once the shard has reached head, get_stream_records is called once per get_records."""
    shard.empty_responses = CALLS_TO_REACH_HEAD

    # Intentionally provide more than one page to ensure
    # the call isn't stopping because there is only one page.
    records = build_get_records_responses(1, 1)
    session.get_stream_records.side_effect = records

    returned_records = shard.get_records()

    assert len(returned_records) == 1
    assert returned_records[0]["meta"]["sequence_number"] == 0
    assert session.get_stream_records.called_once_with(shard.iterator_id)

    assert shard.iterator_type == "at_sequence"
    assert shard.sequence_number == 0


@pytest.mark.parametrize("chain", [
    # === 0 records on every page, from 1 - CALLS_TO_REACH_HEAD + 1 pages
    *[[0] * i for i in range(1, CALLS_TO_REACH_HEAD + 1)],

    # === 1 record on every page, from 1 - CALLS_TO_REACH_HEAD + 1 pages
    *[[1] * i for i in range(1, CALLS_TO_REACH_HEAD + 1)],

    # === 1 record, CALLS_TO_REACH_HEAD - 1 pages ===
    *[([0] * i) + [1] + ([0] * (CALLS_TO_REACH_HEAD - 2 - i)) for i in range(CALLS_TO_REACH_HEAD - 1)],

    # === 1 record, CALLS_TO_REACH_HEAD pages ===
    *[([0] * i) + [1] + ([0] * (CALLS_TO_REACH_HEAD - 1 - i)) for i in range(CALLS_TO_REACH_HEAD)],

    # === 1 record, CALLS_TO_REACH_HEAD + 1 pages ===
    *[([0] * i) + [1] + ([0] * (CALLS_TO_REACH_HEAD - 0 - i)) for i in range(CALLS_TO_REACH_HEAD + 1)],

    # Contains every permutation of 3-page runs:
    # (first value is 00 to align the comment)
    # 0, 0, 0
    #    0, 0, 1
    #       0, 1, 1
    #          1, 1, 1
    #             1, 1, 0
    #                1, 0, 1
    #                   0, 1, 0
    #                      1, 0, 0
    [00, 0, 0, 1, 1, 1, 0, 1, 0, 0],

])
def test_get_records_shard(chain, shard, session):
    """Catchup logic is applied until the CALLS_TO_REACH_HEAD limit, or shard is exhausted.

    This holds even when there are non-empty calls in between.  The catch
    up logic will be applied on the next empty response."""
    #
    responses = build_get_records_responses(*chain)
    session.get_stream_records.side_effect = responses

    records = []
    get_records_call_count = 0
    while not shard.exhausted:
        get_records_call_count += 1
        records.extend(shard.get_records())

    # Calls to shard.get_records() to exhaust the shard
    assert get_records_call_count == expected_get_calls(chain)

    # Call until exhausted, means we always reach the end of the chain
    assert session.get_stream_records.call_count == len(chain)
    assert len(records) == sum(chain)


@pytest.mark.parametrize("initial_sequence_number", [None, "sequence-number"])
@pytest.mark.parametrize("record_count", [0, 1, 2])
def test_apply_records(initial_sequence_number, record_count, session):
    # Temporarily ignoring that an iterator should never be "latest" and have a sequence_number..
    shard = Shard(stream_arn="stream-arn", shard_id="shard-id", iterator_type="initial-iterator-type",
                  sequence_number=initial_sequence_number, session=session)

    records = [record_with(key=True, sequence_number=i) for i in range(record_count)]
    response = {
        "Records": records,
        "NextShardIterator": "next-iterator-id"
    }
    shard._apply_get_records_response(response)
    session.get_stream_records.assert_not_called()

    if records:
        if initial_sequence_number:
            # Don't overwrite; found records but already had a sequence_number
            assert shard.iterator_type == "initial-iterator-type"
            assert shard.sequence_number == initial_sequence_number
        else:
            # Remember first sequence_number; found records and no existing sequence_number
            assert shard.iterator_type == "at_sequence"
            assert shard.sequence_number == records[0]["dynamodb"]["SequenceNumber"] == 0
        assert shard.empty_responses == 0
    else:
        # No records, no change
        assert shard.iterator_type == "initial-iterator-type"
        assert shard.sequence_number == initial_sequence_number
        assert shard.empty_responses == 1


@pytest.mark.parametrize("include", [{"new"}, {"old"}, {"old", "new"}, {"key"}])
def test_reformat_record(include):
    raw = record_with(**{field: True for field in include})

    record = reformat_record(raw)
    renames = {
        "new": "NewImage",
        "old": "OldImage",
        "key": "Keys"
    }
    for field in {"new", "old", "key"}:
        if field in include:
            assert record[field] is raw["dynamodb"][renames[field]]
        else:
            assert record[field] is None

    assert record["meta"]["created_at"].timestamp == raw["dynamodb"]["ApproximateCreationDateTime"]
    assert record["meta"]["event"]["type"] == raw["eventName"].lower()


def test_unpack_empty_shards_list(session):
    assert unpack_shards([], "stream-arn", session) == {}


def test_unpack_shards_from_token(session):
    # multiple roots, 1:1 and 1:2 relations
    shards = build_shards(5, {0: 1, 2: [3, 4]}, session, stream_arn="stream_arn")
    by_id = {shard.shard_id: shard for shard in shards}

    # unpacking shouldn't rely on ordering over the wire
    tokens = [shard.token for shard in shards]
    random.shuffle(tokens)
    unpacked = unpack_shards(tokens, "stream_arn", session)

    assert unpacked == by_id


def test_unpack_shards_from_describe_stream(session):
    # multiple roots, 1:1 and 1:2 relations
    shards = stream_description(5, {0: 1, 2: [3, 4]})["Shards"]
    by_id = {shard["ShardId"]: shard for shard in shards}

    # unpacking shouldn't rely on ordering over the wire
    random.shuffle(shards)
    unpacked = unpack_shards(shards, "stream_arn", session=session)

    assert by_id.keys() == unpacked.keys()
    for shard_id, shard in unpacked.items():
        if shard.parent is None:
            assert "ParentShardId" not in by_id[shard_id]
        else:
            assert shard.parent.shard_id == by_id[shard_id].get("ParentShardId")
