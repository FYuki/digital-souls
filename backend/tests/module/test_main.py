import importlib
from unittest.mock import patch


def test_load_dotenv_is_called_when_app_main_is_imported():
    with patch("dotenv.load_dotenv") as mock_load:
        import app.main as main_module

        importlib.reload(main_module)

    mock_load.assert_called()
