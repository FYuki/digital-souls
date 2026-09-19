"""CUDA非搭載CIでも設定・失敗復旧・Graph所有期間を検証する。"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from irodori_service import cuda_graph
from irodori_service.config import ServiceConfig, load_config


def test_graph_is_opt_in_and_rejects_ambiguous_configuration(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("DS_IRODORI_CUDA_GRAPH", raising=False)
    assert not load_config().cuda_graph
    monkeypatch.setenv("DS_IRODORI_CUDA_GRAPH", "true")
    assert load_config().cuda_graph
    for value in ("1", "TRUE", "", "yes"):
        monkeypatch.setenv("DS_IRODORI_CUDA_GRAPH", value)
        with pytest.raises(ValueError, match="DS_IRODORI_CUDA_GRAPH"):
            load_config()
    with pytest.raises(ValueError, match="boolean"):
        ServiceConfig(tmp_path, tmp_path, cuda_graph="false")


@pytest.mark.parametrize("failure", [False, True])
def test_sampler_restores_patches_and_releases_request_graph(monkeypatch, failure):
    original = lambda **kwargs: kwargs
    embedding = object()
    model = SimpleNamespace(forward_with_encoded_conditions=original)
    module = SimpleNamespace(get_timestep_embedding=embedding)
    graphs = []
    synchronized = []
    torch = SimpleNamespace(cuda=SimpleNamespace(synchronize=lambda: synchronized.append(True)))

    class Graph:
        def __init__(self, *_args):
            self.entries = {"previous_text": object()}
            graphs.append(self)

    monkeypatch.setattr(cuda_graph, "RequestGraph", Graph)

    def sampler(current, value):
        assert current.forward_with_encoded_conditions is graphs[-1]
        assert module.get_timestep_embedding is not embedding
        if failure:
            raise RuntimeError("capture_failed")
        return value

    wrapped = cuda_graph.wrap_sampler(torch, module, sampler)
    for _ in range(2):
        if failure:
            with pytest.raises(RuntimeError, match="capture_failed"):
                wrapped(model, 42)
        else:
            assert wrapped(model, 42) == 42
        assert model.forward_with_encoded_conditions is original
        assert module.get_timestep_embedding is embedding
        assert all(not graph.entries for graph in graphs)
    assert graphs[0] is not graphs[1]
    assert len(synchronized) == (0 if failure else 2)


@pytest.mark.parametrize("restriction", ["capacity", "memory", "unsupported", "shapes"])
def test_ineligible_inputs_use_eager_before_capture(restriction):
    class Tensor:
        is_cuda = True
        requires_grad = False
        shape = (1,)
        dtype = "bf16"
        device = "cuda:0"

        def stride(self):
            return (1,)

        def numel(self):
            return cuda_graph.MAX_INPUT_BYTES if restriction == "capacity" else 1

        def element_size(self):
            return 2

    def no_capture(*_args, **_kwargs):
        raise AssertionError("capture must not run")

    torch = SimpleNamespace(
        Tensor=Tensor,
        cuda=SimpleNamespace(
            mem_get_info=lambda: (0, 0) if restriction == "memory" else (2**40, 2**40),
            Stream=no_capture,
        ),
    )
    calls = []
    def forward(**kwargs):
        calls.append(kwargs)
        return "eager"
    graph = cuda_graph.RequestGraph(torch, forward)
    if restriction == "shapes":
        graph.entries.update({1: None, 2: None})
    value = object() if restriction == "unsupported" else Tensor()
    assert graph(x=value) == "eager"
    assert calls == [{"x": value}]


def test_fixed_model_cfg_input_reaches_capture_with_available_vram():
    # 固定モデルの実測入力合計。64/128MiBでは通常のCFGがeagerへ戻っていた。
    class Tensor:
        is_cuda = True
        requires_grad = False
        shape = (107161014,)
        dtype = "bf16"
        device = "cuda:0"

        def stride(self):
            return (1,)

        def numel(self):
            return 107161014

        def element_size(self):
            return 2

        def clone(self):
            return self

    class CaptureEntered(Exception):
        pass

    def capture_stream():
        raise CaptureEntered

    torch = SimpleNamespace(
        Tensor=Tensor,
        cuda=SimpleNamespace(
            mem_get_info=lambda: (4 * 1024**3, 16 * 1024**3),
            Stream=capture_stream,
        ),
    )
    graph = cuda_graph.RequestGraph(torch, lambda **kwargs: "eager")
    with pytest.raises(CaptureEntered):
        graph(x=Tensor())
