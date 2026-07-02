from forge.appserver import detect_appserver


def test_repo_yml_start_wins():
    spec = detect_appserver("playwright:\n  start: yarn dev\n  port: 4000\n", None)
    assert spec.ok and spec.port == 4000
    assert spec.start_argv == ["sh", "-lc", "PORT=4000 yarn dev"]


def test_repo_yml_start_without_port_uses_default():
    spec = detect_appserver("playwright:\n  start: yarn dev\n", None)
    assert spec.ok and spec.port == 3000


def test_package_json_dev_script():
    spec = detect_appserver(None, '{"scripts":{"dev":"next dev"}}')
    assert spec.ok and spec.port == 3000
    assert spec.start_argv == ["sh", "-lc", "PORT=3000 npm run dev"]


def test_package_json_start_fallback():
    spec = detect_appserver(None, '{"scripts":{"start":"node server.js"}}')
    assert spec.start_argv == ["sh", "-lc", "PORT=3000 npm run start"]


def test_no_server_detected():
    spec = detect_appserver(None, '{"scripts":{"test":"jest"}}')
    assert not spec.ok


def test_no_inputs():
    assert not detect_appserver(None, None).ok
