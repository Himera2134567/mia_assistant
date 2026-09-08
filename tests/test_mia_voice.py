from mia_voice import native_model_path, vosk_model_ready


def test_vosk_model_ready_validates_required_structure(tmp_path):
    model = tmp_path / "vosk-ru"
    required = (
        model / "am" / "final.mdl",
        model / "conf" / "model.conf",
        model / "graph" / "HCLr.fst",
        model / "graph" / "Gr.fst",
    )
    assert not vosk_model_ready(model)
    for path in required:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test")
    assert vosk_model_ready(model)


def test_native_model_path_points_to_existing_directory(tmp_path):
    model = tmp_path / "модель"
    model.mkdir()
    native = native_model_path(model)
    assert native
