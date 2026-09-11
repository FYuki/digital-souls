import { invoke } from "@tauri-apps/api/core";
import { Live2DModel } from "@naari3/pixi-live2d-display/cubism5";
import * as PIXI from "pixi.js";
// TauriのCSP下ではevalを使わないPixiJSの実装を登録する。
import "pixi.js/unsafe-eval";

const MODEL_URL =
  "https://raw.githubusercontent.com/Live2D/CubismWebSamples/develop/Samples/Resources/Hiyori/Hiyori.model3.json";
const MODEL_BASE_URL = MODEL_URL.slice(0, MODEL_URL.lastIndexOf("/") + 1);

type StatusCallback = (status: string, detail?: string) => void;

type Live2DHandle = {
  destroy: () => void;
};

type ModelManifest = {
  FileReferences?: {
    Moc?: string;
    Textures?: string[];
  };
};

async function reportStatus(status: string, detail: string): Promise<void> {
  try {
    await invoke("report_live2d_status", { status, detail });
  } catch {
    // Vite単体プレビューではTauri IPCが存在しないため無視する。
  }
}

async function updateStatus(
  onStatus: StatusCallback,
  status: string,
  detail: string,
): Promise<void> {
  onStatus(status, detail);
  await reportStatus(status, detail);
}

async function fetchWithTimeout(url: string, timeoutMs = 15_000): Promise<Response> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { signal: controller.signal, cache: "no-store" });
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText}: ${url}`);
    }
    return response;
  } finally {
    window.clearTimeout(timeout);
  }
}

async function preflightModel(onStatus: StatusCallback): Promise<void> {
  await updateStatus(onStatus, "preflight", "Hiyori manifest を取得中");
  const manifestResponse = await fetchWithTimeout(MODEL_URL);
  const manifest = (await manifestResponse.json()) as ModelManifest;

  const moc = manifest.FileReferences?.Moc;
  const firstTexture = manifest.FileReferences?.Textures?.[0];
  if (!moc || !firstTexture) {
    throw new Error("Hiyori manifest にMocまたはTexture定義がありません");
  }

  await updateStatus(onStatus, "preflight", `MOC3取得確認: ${moc}`);
  const mocResponse = await fetchWithTimeout(new URL(moc, MODEL_BASE_URL).href);
  const mocBytes = await mocResponse.arrayBuffer();
  if (mocBytes.byteLength === 0) {
    throw new Error("MOC3が空です");
  }

  await updateStatus(onStatus, "preflight", `Texture取得確認: ${firstTexture}`);
  const textureResponse = await fetchWithTimeout(new URL(firstTexture, MODEL_BASE_URL).href);
  const textureBytes = await textureResponse.arrayBuffer();
  if (textureBytes.byteLength === 0) {
    throw new Error("Live2D textureが空です");
  }

  await updateStatus(
    onStatus,
    "runtime-ready",
    `Cubism資産取得OK (moc=${mocBytes.byteLength} bytes, texture=${textureBytes.byteLength} bytes)`,
  );
}

export async function mountLive2D(
  canvas: HTMLCanvasElement,
  onStatus: StatusCallback,
): Promise<Live2DHandle> {
  try {
    if (!window.Live2DCubismCore) {
      throw new Error("Live2D Cubism Core を読み込めませんでした");
    }

    // Live2DModelの自動更新がPixiJSのTickerを利用できるようにする。
    window.PIXI = PIXI;
    Live2DModel.registerTicker(PIXI.Ticker);

    const host = canvas.parentElement;
    if (!host) {
      throw new Error("Live2D canvas host がありません");
    }

    await preflightModel(onStatus);
    await updateStatus(onStatus, "loading", "Cubism 5 rendererでHiyoriを構築中");

    const app = new PIXI.Application();
    await app.init({
      canvas,
      resizeTo: host,
      backgroundAlpha: 0,
      antialias: true,
      autoDensity: true,
      resolution: Math.min(window.devicePixelRatio || 1, 2),
      preference: "webgl",
    });

    const model = await Live2DModel.from(MODEL_URL, {
      autoInteract: false,
    });
    // PixiJS 8の描画コールバックが参照するrendererを明示する。
    model.setRenderer(app.renderer);
    app.stage.addChild(model);

    const baseWidth = Math.max(model.width, 1);
    const baseHeight = Math.max(model.height, 1);
    const fit = () => {
      const width = Math.max(app.screen.width, 1);
      const height = Math.max(app.screen.height, 1);
      const scale = Math.min(width / baseWidth, height / baseHeight) * 0.92;
      model.anchor.set(0.5, 0.5);
      model.scale.set(scale);
      model.x = width / 2;
      model.y = height / 2;
    };

    fit();
    const resizeObserver = new ResizeObserver(() => fit());
    resizeObserver.observe(host);

    try {
      void model.motion("Idle", 0);
    } catch {
      // モーション開始に失敗しても描画確認自体は継続する。
    }

    // WebView2上で実際に描画ループが1回進んだ後にreadyを通知する。
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    app.renderer.render(app.stage);

    // 読込成功だけでREADYにせず、透明背景以外の描画結果があることを確認する。
    const gl = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
    if (!gl) throw new Error("Live2DのWebGLコンテキストを取得できません");
    const pixels = new Uint8Array(gl.drawingBufferWidth * gl.drawingBufferHeight * 4);
    gl.readPixels(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    let visiblePixels = 0;
    for (let i = 3; i < pixels.length; i += 4) {
      if (pixels[i] > 0) visiblePixels++;
    }
    if (visiblePixels === 0) throw new Error("Live2Dの描画結果が透明です");

    const detail = `Live2D ready: ${Math.round(app.renderer.width)}x${Math.round(app.renderer.height)}, pixels=${visiblePixels}`;
    await updateStatus(onStatus, "ready", detail);

    return {
      destroy: () => {
        resizeObserver.disconnect();
        model.destroy({ children: true });
        app.destroy(false);
      },
    };
  } catch (error) {
    const detail = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    await updateStatus(onStatus, "error", detail);
    throw error;
  }
}
