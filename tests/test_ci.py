import yaml


def test_workflow_is_valid_yaml_with_the_required_steps(repo):
    wf = yaml.safe_load((repo / ".github" / "workflows" / "ci.yml").read_text())
    triggers = wf.get("on", wf.get(True))
    assert "push" in triggers and "pull_request" in triggers
    job = next(iter(wf["jobs"].values()))
    assert job["runs-on"] == "ubuntu-latest"
    assert job["strategy"]["matrix"]["python-version"] == ["3.12", "3.13"]
    runs = [s.get("run") for s in job["steps"] if "run" in s]
    assert runs == ['pip install -e ".[dev]"', "ruff check .", "ruff format --check .", "pytest -q"]
