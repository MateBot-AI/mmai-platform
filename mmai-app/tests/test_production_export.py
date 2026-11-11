import re
from pathlib import Path

from tools.export_production import export


def test_sanitised_export_keeps_runtime_assets_and_template(tmp_path):
    root = Path(__file__).resolve().parents[2]
    output = tmp_path / "export"
    export(root, output)
    assert (output / ".dockerignore").is_file()
    assert (output / "mmai-app/data/ALM_Apartments_2026-10-23_features_template.xlsx").is_file()
    assert not list(output.rglob("*.db"))
    assert not (output / "mmai-app/bootstrap/staff_accounts.db").exists()
    assert len(list(output.rglob("*.woff2"))) == 12
    for folder in (output / "mmai-app/client", output / "homebot-app"):
        for css in folder.glob("*.css"):
            for url in re.findall(r"url\(['\"]?([^)'\"\s]+)", css.read_text()):
                if not url.startswith(("data:", "http:", "https:", "#")):
                    assert (css.parent / url.split("?")[0]).is_file(), (css.name, url)
