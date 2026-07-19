import re
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import BaseModel

from mali_app.api import create_app
from mali_app.cli import run
from mali_app.item_writer import ItemWriterResponse
from mali_app.model_gateway import (
    ModelIdentity,
    StreamDelta,
    StreamRequest,
    StructuredRequest,
)


def test_api_drives_placement_target_and_check_to_audited_completion(
    tmp_path: Path,
) -> None:
    database = str(tmp_path / "api.db")
    client = TestClient(create_app(database))

    registered = client.post(
        "/v1/learners", json={"learner_id": "api-learner", "display_name": "Ada"}
    )
    assert registered.status_code == 201
    assert not registered.json()["placed"]

    assert client.post("/v1/learners/api-learner/placement").status_code == 200
    for _ in range(5):
        _answer_current_question(client)
    placed = client.get("/v1/learners/api-learner/progress")
    assert placed.json()["placed"]

    targeted = client.post("/v1/learners/api-learner/targets/equal-halves")
    assert targeted.status_code == 200
    assert targeted.json()["current_skill"] == "equal-halves"
    assert client.post("/v1/learners/api-learner/check").status_code == 200
    for _ in range(3):
        _answer_current_question(client)
    completed = client.get("/v1/learners/api-learner/progress")

    assert completed.json()["mastered"] == ["Equal halves"]
    assert run(("audit", "--database", database, "--learner", "api-learner")) == 0


def test_api_returns_a_product_safe_refusal_for_a_target_before_placement(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(str(tmp_path / "refusal.db")))
    client.post(
        "/v1/learners",
        json={"learner_id": "new-learner", "display_name": "Ada"},
    )

    response = client.post("/v1/learners/new-learner/targets/equal-halves")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "placement_required"
    assert "short check" in response.json()["detail"]["message"]


def test_question_endpoint_uses_item_writer_only_when_its_feature_flag_is_on(
    tmp_path: Path,
) -> None:
    gateway = ParameterEchoGateway()
    client = TestClient(
        create_app(
            str(tmp_path / "item-writer.db"),
            enable_item_writer=True,
            model_gateway=gateway,
        )
    )
    client.post(
        "/v1/learners", json={"learner_id": "writer-learner", "display_name": "Ada"}
    )
    assert client.post("/v1/learners/writer-learner/placement").status_code == 200

    question = client.get("/v1/learners/writer-learner/question")

    assert question.status_code == 200
    assert re.fullmatch(r"What is \d+/2 \+ 1/2\?", question.json()["prompt"])
    assert len(gateway.request_inputs) == 1


def _answer_current_question(client: TestClient) -> None:
    question = client.get("/v1/learners/api-learner/question")
    assert question.status_code == 200
    payload = question.json()
    response = client.post(
        "/v1/learners/api-learner/answers",
        json={
            "question_id": payload["question_id"],
            "answer": _answer_for(payload["prompt"]),
        },
    )
    assert response.status_code == 200


def _answer_for(prompt: str) -> str:
    match = re.fullmatch(r"What is (\d+)/2 \+ 1/2\?", prompt)
    assert match is not None
    value = Fraction(int(match.group(1)), 2) + Fraction(1, 2)
    return str(value.numerator) if value.denominator == 1 else str(value)


class ParameterEchoGateway:
    identity = ModelIdentity("fixture", "parameter-echo")

    def __init__(self) -> None:
        self.request_inputs: list[str] = []

    def stream(self, request: StreamRequest) -> Iterator[StreamDelta]:
        return iter(())

    def structured[ResultT: BaseModel](
        self, request: StructuredRequest[ResultT]
    ) -> ResultT:
        self.request_inputs.append(request.input)
        match = re.search(r"- numerator: (\d+)", request.input)
        assert match is not None
        return request.result_type.model_validate(
            ItemWriterResponse(
                question_text=f"What is {match.group(1)}/2 + 1/2?"
            ).model_dump(mode="json")
        )
