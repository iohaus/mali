from mali.actions import Actor, ProposeTarget
from mali.curriculum import Curriculum, Skill
from mali.desk import TutorDesk
from mali.ids import checkpoint_id, learner_id, skill_code
from mali.plans import ProgressWrite
from mali.policy import POLICY_V1
from mali.progress import Progress
from mali.rules import RefusalReason, Refused
from mali.snapshot import Snapshot


def _snapshot(placed: bool = True) -> Snapshot:
    parts = Skill(skill_code("parts"), 0, "Parts", "Understand parts.")
    add = Skill(skill_code("add"), 1, "Add", "Add parts.")
    curriculum = Curriculum.load((parts, add), (("add", ("parts",)),))
    progress = Progress(
        learner_id("desk-learner"), curriculum.version, 0, placed, None, 0, curriculum
    )
    return Snapshot(progress, None, POLICY_V1, checkpoint_id("desk-check"))


def test_plan_rechecks_rules_before_producing_writes() -> None:
    snapshot = _snapshot()
    plan = TutorDesk.plan(ProposeTarget(skill_code("parts")), snapshot, Actor.STUDENT)

    assert not isinstance(plan, Refused)
    assert isinstance(plan.writes[0], ProgressWrite)
    assert plan.writes[0].progress.target == skill_code("parts")


def test_refused_actions_do_not_produce_a_plan() -> None:
    plan = TutorDesk.plan(ProposeTarget(skill_code("add")), _snapshot(), Actor.STUDENT)

    assert isinstance(plan, Refused)
    assert plan.reason is RefusalReason.NOT_READY_YET


def test_available_lists_ready_targets_with_their_path() -> None:
    available = TutorDesk.available(_snapshot())

    assert available.targets == ((skill_code("parts"), (skill_code("parts"),)),)
