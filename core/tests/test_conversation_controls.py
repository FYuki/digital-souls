import pytest

from .conversation_support import turn

pytestmark = pytest.mark.it1


@pytest.mark.parametrize("indices", [[-1], [1], [0, 0], [True]])
def test_invalid_message_exclusions_rejected(indices: list[object]) -> None:
    with pytest.raises(ValueError):
        turn(memory_excluded_indices=indices)
