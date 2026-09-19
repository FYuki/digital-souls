"""固定upstream専用の、要求内だけで有効なRF forwardのCUDA Graph。"""
from __future__ import annotations

import hashlib
import inspect
import importlib
from collections.abc import Callable
from functools import wraps
from typing import Any

EMBEDDING_SOURCE_SHA256 = "b024c84ae48f89f52992614c845e750ecf385e5537b1661328555200932dac93"
MAX_ENTRIES = 2
MAX_INPUT_BYTES = 64 * 1024 * 1024
FREE_MEMORY_RESERVE = 1024 * 1024 * 1024


def _cached_embedding(torch: Any) -> Callable[..., Any]:
    frequencies: dict[Any, Any] = {}

    def embedding(timestep: Any, dim: int) -> Any:
        assert dim % 2 == 0
        key = (dim, timestep.device)
        if key not in frequencies:
            half = dim // 2
            # upstreamと同じCUDA演算順・dtype。capture前のwarmupで定数転送を終える。
            frequencies[key] = 1000.0 * torch.exp(
                -torch.log(torch.tensor(10000.0, device=timestep.device, dtype=torch.float32))
                * torch.arange(half, device=timestep.device, dtype=torch.float32) / half
            )
        args = timestep[:, None].float() * frequencies[key][None, :]
        return torch.cat([torch.cos(args), torch.sin(args)], dim=-1).to(timestep.dtype)

    return embedding


class RequestGraph:
    """直列GPU worker専用。形状・入力容量・空きVRAMでcapture前に制限する。"""

    def __init__(self, torch: Any, forward: Callable[..., Any]) -> None:
        self.torch = torch
        self.forward = forward
        self.entries: dict[Any, tuple[Any, Any, Any]] = {}

    def _signature(self, value: Any) -> Any:
        if isinstance(value, self.torch.Tensor):
            return ("tensor", tuple(value.shape), tuple(value.stride()), value.dtype, value.device, value.requires_grad)
        if isinstance(value, dict):
            return ("dict", tuple((k, self._signature(v)) for k, v in value.items()))
        if isinstance(value, (list, tuple)):
            return (type(value), tuple(self._signature(v) for v in value))
        if value is None or type(value) in (bool, int, float, str):
            return (type(value), value)
        raise TypeError("unsupported_graph_input")

    def _input_bytes(self, value: Any) -> int:
        if isinstance(value, self.torch.Tensor):
            if not value.is_cuda or value.requires_grad:
                return MAX_INPUT_BYTES + 1
            return int(value.numel() * value.element_size())
        if isinstance(value, dict):
            return sum(self._input_bytes(v) for v in value.values())
        if isinstance(value, (tuple, list)):
            return sum(self._input_bytes(v) for v in value)
        if value is None or type(value) in (bool, int, float, str):
            return 0
        return MAX_INPUT_BYTES + 1

    def _clone(self, value: Any) -> Any:
        if isinstance(value, self.torch.Tensor):
            return value.clone()
        if isinstance(value, dict):
            return {k: self._clone(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._clone(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self._clone(v) for v in value)
        return value

    def _copy(self, target: Any, source: Any) -> None:
        if isinstance(source, self.torch.Tensor):
            target.copy_(source)
        elif isinstance(source, dict):
            for key in source:
                self._copy(target[key], source[key])
        elif isinstance(source, (tuple, list)):
            for a, b in zip(target, source, strict=True):
                self._copy(a, b)

    def __call__(self, **inputs: Any) -> Any:
        try:
            key = self._signature(inputs)
        except TypeError:
            return self.forward(**inputs)
        if key not in self.entries:
            size = self._input_bytes(inputs)
            if size > MAX_INPUT_BYTES or len(self.entries) >= MAX_ENTRIES:
                return self.forward(**inputs)
            free, _total = self.torch.cuda.mem_get_info()
            if free < size + FREE_MEMORY_RESERVE:
                return self.forward(**inputs)
            static = self._clone(inputs)
            stream = self.torch.cuda.Stream()
            stream.wait_stream(self.torch.cuda.current_stream())
            with self.torch.cuda.stream(stream):
                for _ in range(2):
                    self.forward(**static)
            self.torch.cuda.current_stream().wait_stream(stream)
            graph = self.torch.cuda.CUDAGraph()
            # capture失敗後のCUDA contextは再利用せず、既存worker破棄へ例外を渡す。
            with self.torch.cuda.graph(graph):
                output = self.forward(**static)
            self.entries[key] = (static, graph, output)
        static, graph, output = self.entries[key]
        # speaker K/V等は同じstorageのまま更新されるため、毎回すべてコピーする。
        self._copy(static, inputs)
        graph.replay()
        # joint/alternating CFGでは同じ形状の出力を同時参照する。replayで上書きしない。
        return output.clone()


def wrap_sampler(torch: Any, model_module: Any, sampler: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(sampler)
    def sample(model: Any, *args: Any, **kwargs: Any) -> Any:
        forward = model.forward_with_encoded_conditions
        embedding = model_module.get_timestep_embedding
        graph = RequestGraph(torch, forward)
        model.forward_with_encoded_conditions = graph
        model_module.get_timestep_embedding = _cached_embedding(torch)
        try:
            result = sampler(model, *args, **kwargs)
            torch.cuda.synchronize()
            return result
        finally:
            model.forward_with_encoded_conditions = forward
            model_module.get_timestep_embedding = embedding
            graph.entries.clear()

    return sample


def install_cuda_graph_sampler() -> None:
    # 通常起動・CPU単体テストにtorch依存を追加しない。
    torch = importlib.import_module("torch")
    runtime: Any = importlib.import_module("irodori_tts.inference_runtime")
    model = importlib.import_module("irodori_tts.model")

    source = inspect.getsource(model.get_timestep_embedding).encode()
    if hashlib.sha256(source).hexdigest() != EMBEDDING_SOURCE_SHA256:
        raise RuntimeError("irodori_graph_upstream_mismatch")
    if not torch.cuda.is_available():
        raise RuntimeError("irodori_graph_cuda_required")
    runtime.sample_euler_rf_cfg = wrap_sampler(torch, model, runtime.sample_euler_rf_cfg)
