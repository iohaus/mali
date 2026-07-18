from mali.actions import Actor, ProposeTarget
from mali.curriculum import Curriculum, Skill
from mali.desk import TutorDesk
from mali.ids import checkpoint_id, learner_id, skill_code
from mali.policy import POLICY_V1
from mali.progress import Progress
from mali.rules import Refused
from mali.snapshot import Snapshot
from mali.views import evidence_view, progress_map, teacher_summary


def test_product_views_report_only_learner_facts() -> None:
    parts = Skill(skill_code("parts"), 0, "Parts", "Understand parts.")
    add = Skill(skill_code("add"), 1, "Add", "Add parts.")
    curriculum = Curriculum.load((parts, add), (("add", ("parts",)),))
    progress = Progress(
        learner_id("view-learner"), curriculum.version, 0, True, None, 0, curriculum
    )
    snapshot = Snapshot(progress, None, POLICY_V1, checkpoint_id("view-check"))
    plan = TutorDesk.plan(ProposeTarget(skill_code("parts")), snapshot, Actor.STUDENT)
    assert not isinstance(plan, Refused)

    mapped = progress_map(progress, curriculum)
    summary = teacher_summary(progress, curriculum, (plan,))
    evidence = evidence_view((plan,))

    assert mapped.next_up == ("Parts",)
    assert summary.ready_count == 1
    assert evidence[0].actor == "student"
