from abrl.config import API_ORIGIN
from abrl.db import Session, Target, Workflow, new_id
from abrl.schemas import TargetInput, WorkflowInput

FIXTURE_ID = "00000000-0000-4000-a000-000000000001"
SCHEDULE_FIXTURE_ID = "00000000-0000-4000-a000-000000000010"
WORKFLOWS = [
    (
        "Landing check",
        [{"action": "navigate", "url": "/lab"}, {"action": "assert_visible", "selector": "#lab-ready"}],
    ),
    (
        "Interaction",
        [
            {"action": "navigate", "url": "/lab"},
            {"action": "click", "selector": "#form-link"},
            {"action": "assert_visible", "selector": "#sample-name"},
        ],
    ),
    (
        "Sample form",
        [
            {"action": "navigate", "url": "/lab/form"},
            {"action": "fill", "selector": "#sample-name", "value": "Sample Researcher"},
            {"action": "select", "selector": "#sample-topic", "value": "research"},
            {"action": "click", "selector": "#sample-submit"},
            {"action": "assert_text", "selector": "#form-success", "value": "Sample received"},
        ],
    ),
    (
        "Multi-page journey",
        [
            {"action": "navigate", "url": "/lab"},
            {"action": "click", "selector": "#next-link"},
            {"action": "click", "selector": "#finish-link"},
            {"action": "assert_visible", "selector": "#journey-complete"},
        ],
    ),
    (
        "Repeat visit",
        [
            {"action": "navigate", "url": "/lab/repeat"},
            {"action": "navigate", "url": "/lab/repeat"},
            {"action": "assert_text", "selector": "#visit-count", "value": "2"},
        ],
    ),
]


def seed_starter():
    from abrl.db import TargetVersion, WorkflowVersion, record

    with Session.begin() as session:
        for target_id, name, origin in (
            (FIXTURE_ID, "ABRL controlled test site", API_ORIGIN),
            (SCHEDULE_FIXTURE_ID, "ABRL schedule test site", "https://schedule.fixture.test"),
        ):
            if session.get(Target, target_id) is not None:
                continue
            target = Target(id=target_id, version=1, verified_version=1, proof_token=new_id())
            session.add(target)
            session.flush()
            data = TargetInput(name=name, origin=origin, authorized=True, rate_limit=60)
            session.add(TargetVersion(target_id=target.id, version=1, config=data.model_dump()))
            record(session, "target_created", target_id=target.id, version=1)
        ids = []
        for index, (name, steps) in enumerate(WORKFLOWS, start=2):
            id = f"00000000-0000-4000-a000-{index:012d}"
            ids.append(id)
            if session.get(Workflow, id) is None:
                session.add(Workflow(id=id, version=1))
                session.flush()
                session.add(
                    WorkflowVersion(
                        workflow_id=id, version=1, config=WorkflowInput(name=name, steps=steps).model_dump()
                    )
                )
                record(session, "workflow_created", workflow_id=id, version=1)
        return {"target_id": FIXTURE_ID, "workflow_ids": ids}


if __name__ == "__main__":
    print(seed_starter())
