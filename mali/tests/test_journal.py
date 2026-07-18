from dataclasses import replace

import pytest

from mali.actions import Actor, ClearTarget, ProposeTarget
from mali.curriculum import Curriculum, Skill
from mali.desk import TutorDesk
from mali.errors import JournalCorruption
from mali.ids import checkpoint_id, learner_id, skill_code
from mali.journal import Journal
from mali.plans import ProgressWrite
from mali.policy import POLICY_V1
from mali.progress import Progress
from mali.rules import Refused
from mali.snapshot import Snapshot


def _snapshot() -> Snapshot:
    parts = Skill(skill_code("parts"), 0, "Parts", "Understand parts.")
    curriculum = Curriculum.load((parts,), ())
    progress = Progress(
        learner_id("journal-learner"), curriculum.version, 0, True, None, 0, curriculum
    )
    return Snapshot(progress, None, POLICY_V1, checkpoint_id("journal-check"))


def test_replay_matches_the_planned_progress() -> None:
    snapshot = _snapshot()
    plan = TutorDesk.plan(ProposeTarget(skill_code("parts")), snapshot, Actor.STUDENT)
    assert not isinstance(plan, Refused)

    assert Journal.replay(snapshot.progress, (plan,)).target == skill_code("parts")


def test_replay_rejects_a_forged_version() -> None:
    snapshot = _snapshot()
    plan = TutorDesk.plan(ProposeTarget(skill_code("parts")), snapshot, Actor.STUDENT)
    assert not isinstance(plan, Refused)
    forged = replace(plan, entry=replace(plan.entry, prior_version=5))

    with pytest.raises(JournalCorruption, match="prior version"):
        Journal.replay(snapshot.progress, (forged,))


def test_replay_rejects_reordered_or_truncated_version_chains() -> None:
    snapshot = _snapshot()
    first = TutorDesk.plan(ProposeTarget(skill_code("parts")), snapshot, Actor.STUDENT)
    assert not isinstance(first, Refused)
    first_write = first.writes[0]
    assert isinstance(first_write, ProgressWrite)
    advanced = Snapshot(
        first_write.progress, None, POLICY_V1, checkpoint_id("journal-check-2")
    )
    second = TutorDesk.plan(ClearTarget(), advanced, Actor.ENGINE)
    assert not isinstance(second, Refused)

    assert Journal.replay(snapshot.progress, (first, second)).target is None
    with pytest.raises(JournalCorruption):
        Journal.replay(snapshot.progress, (second, first))
