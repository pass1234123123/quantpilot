"""NATS subject builders (PRD 13.6)."""


def node_events_subject(node_id: str) -> str:
    return f"node.{node_id}.events"


def node_heartbeat_subject(node_id: str) -> str:
    return f"node.{node_id}.heartbeat"


def node_commands_subject(node_id: str) -> str:
    return f"node.{node_id}.commands"


def account_snapshot_subject(account_id: str) -> str:
    return f"account.{account_id}.snapshot"


def system_alerts_subject() -> str:
    return "system.alerts"
