from hypothesis import given
from hypothesis import strategies as st

from mali.actions import Actor, ProposeTarget
from mali.curriculum import Curriculum, Skill
from mali.desk import TutorDesk
from mali.ids import checkpoint_id, learner_id, skill_code
from mali.journal import Journal
from mali.plans import ProgressWrite
from mali.policy import POLICY_V1
from mali.progress import Progress
from mali.rules import Refused
from mali.snapshot import Snapshot


@given(st.booleans())
def test_target_plans_preserve_valid_progress(placed: bool) -> None:
    skill = Skill(skill_code("parts"), 0, "Parts", "Understand parts.")
    curriculum = Curriculum.load((skill,), ())
    progress = Progress(
        learner_id("property-learner"),
        curriculum.version,
        0,
        placed,
        None,
        0,
        curriculum,
    )
    snapshot = Snapshot(progress, None, POLICY_V1, checkpoint_id("property-check"))
    outcome = TutorDesk.plan(
        ProposeTarget(skill_code("parts")), snapshot, Actor.STUDENT
    )

    if isinstance(outcome, Refused):
        assert not placed
    else:
        write = next(
            write for write in outcome.writes if isinstance(write, ProgressWrite)
        )
        assert write.progress.curriculum.is_reachable(write.progress.mask)
        assert Journal.replay(progress, (outcome,)) == write.progress
