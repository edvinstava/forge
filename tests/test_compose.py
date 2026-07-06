from forge import compose


def test_project_name():
    assert compose.project_name("abc123") == "forge-abc123"


def test_up_cmd():
    assert compose.up_cmd("forge-x", ["a.yml", "b.yml"]) == [
        "docker", "compose", "-p", "forge-x",
        "-f", "a.yml", "-f", "b.yml", "up", "-d", "--remove-orphans"]


def test_exec_cmd():
    assert compose.exec_cmd("forge-x", ["a.yml"], "web", ["ls", "-la"], "/work") == [
        "docker", "compose", "-p", "forge-x", "-f", "a.yml",
        "exec", "-T", "-w", "/work", "web", "ls", "-la"]


def test_exec_cmd_env_keys_are_name_only():
    # Per-exec secrets travel as `-e KEY` (name only): docker forwards the
    # value from the client process env, so it never appears in argv / `ps`.
    assert compose.exec_cmd("forge-x", ["a.yml"], "forge", ["git", "push"],
                            "/work", env_keys=("GH_TOKEN",)) == [
        "docker", "compose", "-p", "forge-x", "-f", "a.yml",
        "exec", "-T", "-w", "/work", "-e", "GH_TOKEN", "forge", "git", "push"]


def test_port_cmd():
    assert compose.port_cmd("forge-x", ["a.yml"], "web", 3000)[-3:] == \
        ["port", "web", "3000"]


def test_restart_cmd_targets_one_service():
    # Self-heal restarts ONLY the web service (leaves the worker + supabase up).
    assert compose.restart_cmd("forge-x", ["a.yml"], "web") == [
        "docker", "compose", "-p", "forge-x", "-f", "a.yml", "restart", "web"]


def test_down_cmd_drops_volumes():
    cmd = compose.down_cmd("forge-x", ["a.yml"])
    assert "down" in cmd and "-v" in cmd


def test_stop_cmd_stops_without_removing():
    from forge.compose import stop_cmd
    cmd = stop_cmd("forge-r1", ["/x/forge-compose.yml"])
    assert cmd == ["docker", "compose", "-p", "forge-r1",
                   "-f", "/x/forge-compose.yml", "stop"]
    assert "down" not in cmd and "-v" not in cmd


def test_start_cmd_starts_existing():
    from forge.compose import start_cmd
    cmd = start_cmd("forge-r1", ["/x/forge-compose.yml"])
    assert cmd == ["docker", "compose", "-p", "forge-r1",
                   "-f", "/x/forge-compose.yml", "start"]
