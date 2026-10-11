import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from reins_sdk.pair import create_code, main


class PairCodeTests(unittest.TestCase):
    def test_cli_can_generate_before_task_is_assigned(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pair.token"
            with patch("sys.argv", ["reins_sdk.pair", "--output", str(path)]):
                with redirect_stdout(StringIO()) as output:
                    main()
            self.assertIn("Reins operator will pair this code", output.getvalue())
            self.assertTrue(path.exists())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_cli_can_check_customer_without_task_id(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pair.token"
            create_code(path)
            with patch("sys.argv", ["reins_sdk.pair", "--output", str(path), "--check"]):
                with patch("reins_sdk.pair.ReinsClient") as client:
                    client.return_value.pairing.return_value = {"customer_id": "demo-customer"}
                    with redirect_stdout(StringIO()) as output:
                        main()
            self.assertIn("Connected to Reins customer: demo-customer", output.getvalue())

    def test_private_code_is_stable_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pair.token"
            first = create_code(path)
            self.assertGreaterEqual(len(first), 43)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(first, create_code(path))

    def test_public_or_symlinked_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pair.token"
            create_code(path)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                create_code(path)
            path.unlink()
            target = Path(folder) / "target"
            target.write_text("secret")
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                create_code(path)


if __name__ == "__main__":
    unittest.main()
