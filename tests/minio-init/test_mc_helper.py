import subprocess

from conftest import mc_error

from tasks import _mc


def _result(rc: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["mc"], rc, stdout=stdout, stderr=stderr)


def test_error_text_keeps_message_and_server_cause():
    stdout = mc_error(
        "Unable to make user/group policy association.",
        "The specified group does not exist.",
    )
    assert _mc.error_text(_result(1, stdout, "\n")) == (
        "Unable to make user/group policy association - The specified group does not exist"
    )


def test_error_text_accepts_a_plain_string_error():
    assert _mc.error_text(_result(1, '{"status": "error", "error": "boom"}')) == "boom"


def test_error_text_falls_back_to_stderr_then_exit_code():
    assert _mc.error_text(_result(1, "not json", "mc: <ERROR> no route\n")) == "mc: <ERROR> no route"
    assert _mc.error_text(_result(3, "", "")) == "mc exited with code 3"


def test_iter_json_reads_compact_lines_and_indented_documents():
    text = '{"a": 1}\n{\n "b": 2\n}\n{"c": 3}'
    assert list(_mc.iter_json(text)) == [{"a": 1}, {"b": 2}, {"c": 3}]


def test_mc_puts_the_readable_reason_into_stderr(mc):
    mc.fail("admin", "user", "add", message="Unable to add new user.", cause="The secret key is invalid.")
    result = _mc.mc(["admin", "user", "add", "minio", "app", "x"])
    assert result.returncode == 1
    assert result.stderr == "Unable to add new user - The secret key is invalid"
