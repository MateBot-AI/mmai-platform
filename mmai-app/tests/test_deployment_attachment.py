"""Production database attachments reference a separately approved cluster."""
from pathlib import Path


def test_managed_database_attachment_does_not_claim_to_provision_a_cluster():
    root = Path(__file__).resolve().parents[1]
    spec = (root / ".do" / "app.yaml").read_text()
    database = spec.split("databases:", 1)[1].split("services:", 1)[0]
    assert "production: true" in database
    assert "cluster_name: replace-with-approved-sydney-cluster" in database
    assert "size:" not in database and "num_nodes:" not in database
    assert "do not apply before resource approval" in spec
    assert "deploy_on_push: false" in spec
    assert "instance_count: 1" in spec
    guide = (root / "DEPLOY_DIGITALOCEAN.md").read_text()
    assert "not highly available" in guide
    assert "replace `replace-with-approved-sydney-cluster`" in guide
