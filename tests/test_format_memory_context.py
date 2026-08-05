from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


_HOOK_PATH = Path(__file__).parents[1] / "plugins" / "system" / "hooks" / "format_memory_context.py"
_SPEC = spec_from_file_location("format_memory_context", _HOOK_PATH)
_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_working_directory_document_includes_path():
    result = _MODULE.hook(
        "PreUserMessage",
        {
            "key_documents": [
                {
                    "file_name": "DriFox",
                    "display": "D:/work/DriFox",
                    "is_wd": True,
                    "is_url": False,
                }
            ]
        },
    )

    assert "DriFox（工作目录: D:/work/DriFox）" in result
