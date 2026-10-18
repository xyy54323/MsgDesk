import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_msgdesk import build_repo


class TraceContextTests(unittest.TestCase):
    def test_job_context_is_per_thread(self):
        with TemporaryDirectory() as tmp:
            repo = build_repo(Path(tmp))
            seen = {}
            barrier = threading.Barrier(2)

            def worker(name):
                repo.current_job_id = name
                repo.current_source_id = name + "-src"
                barrier.wait()
                seen[name] = (repo.current_job_id, repo.current_source_id)

            threads = [
                threading.Thread(target=worker, args=(name,))
                for name in ("a", "b")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(seen["a"], ("a", "a-src"))
            self.assertEqual(seen["b"], ("b", "b-src"))


if __name__ == "__main__":
    unittest.main()
