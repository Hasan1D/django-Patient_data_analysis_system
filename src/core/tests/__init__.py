from pathlib import Path


def load_tests(loader, tests, pattern):
    tests_dir = Path(__file__).resolve().parent
    src_dir = tests_dir.parent.parent
    return loader.discover(
        start_dir=str(tests_dir),
        pattern=pattern or "test*.py",
        top_level_dir=str(src_dir),
    )
