"""One checker invocation per process. Checker stdout never contaminates the RPC response."""

import contextlib
import importlib.util
import inspect
import json
import sys


def run(request):
    filename, factory_name = request["entrypoint"].rsplit(":", 1)
    from pathlib import Path

    sys.path.insert(0, str(Path(filename).parent))
    spec = importlib.util.spec_from_file_location("author_checker", filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checker = getattr(module, factory_name)(request["context"])
    for name, args in [("check_service", ()), ("put_flag", ("flag",)), ("get_flag", ())]:
        inspect.signature(getattr(checker, name)).bind(*args)
    if request["operation"] == "validate":
        return {"status": "up", "code": "VALID"}
    available = checker.check_service()
    if type(available) is not bool:
        raise TypeError("check_service must return bool")
    if not available:
        return {"status": "down", "code": "SERVICE_UNAVAILABLE"}
    if request["operation"] == "health":
        return {"status": "up", "code": "OK"}
    if request["operation"] == "plant":
        if checker.put_flag(request["flag"]) is not None:
            raise TypeError("put_flag must return None")
    actual = checker.get_flag()
    if not isinstance(actual, str):
        raise TypeError("get_flag must return str")
    if actual != request["flag"]:
        return {"status": "corrupt", "code": "FLAG_MISMATCH"}
    return {"status": "up", "code": "OK"}


def main():
    request = json.load(sys.stdin)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = run(request)
    except Exception as exc:
        result = {"status": "checker_error", "code": type(exc).__name__}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
