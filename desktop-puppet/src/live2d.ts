import { invoke } from "@tauri-apps/api/core";

const MODEL_URL =
  "https://raw.githubusercontent.com/Live2D/CubismWebSamples/develop/Samples/Resources/Hiyori/Hiyori.model3.json";

type StatusCallback = (status: string, detail?: string) => void;

type Live2DHandle = {
  destroy: () => void;
};

async function reportStatus(status: string, detail: string): Promise<void> {
  try {
    await invoke("report_live2d_status", { status, detail });
  } catch {
    // Vite単体プレビューではTauri IPCが存在しないため無視する。
  }
}

export async function mountLive2D(
  canvas: HTMLCanvasElement,
  onStatus: StatusCallback,
): Promise<Live2DHandle> {
  const PIXI = window.PIXI;
  if (!window.Live2DCubismCore || !PIXI?.live2d?.Live2DModel) {
    const detail = "Cubism Core または pixi-live2d-display を読み込めませんでした";
    onStatus("error", detail);
    await reportStatus("error", detail);
    throw new Error(detail);
  }

  onStatus("loading", "Hiyori.model3.json を読み込み中");
  await reportStatus("loading", "Hiyori.model3.json を読み込み中");

  const host = canvas.parentElement;
  if (!host) {
    throw new Error("Live2D canvas host がありません");
  }

  const app = new PIXI.Application({
    view: canvas,
    resizeTo: host,
    transparent: true,
    antialias: true,
    autoDensity: true,
    resolution: Math.min(window.devicePixelRatio || 1, 2),
  });

  const model = await PIXI.live2d.Live2DModel.from(MODEL_URL, {
    autoInteract: false,
  });
  app.stage.addChild(model);

  const fit = () => {
    const width = Math.max(app.renderer.width, 1);
    const height = Math.max(app.renderer.height, 1);
    const scale = Math.min(width / model.width, height / model.height) * 0.92;
    model.anchor.set(0.5, 0.5);
    model.scale.set(scale);
    model.x = width / 2;
    model.y = height / 2;
  };

  fit();
  const resizeObserver = new ResizeObserver(() => fit());
  resizeObserver.observe(host);

  try {
    model.motion("Idle", 0);
  } catch {
    // モーション開始に失敗しても描画確認自体は継続する。
  }

  // 1フレーム明示描画してからreadyを通知する。
  app.renderer.render(app.stage);
  const detail = `Live2D ready: ${Math.round(app.renderer.width)}x${Math.round(app.renderer.height)}`;
  onStatus("ready", detail);
  await reportStatus("ready", detail);

  return {
    destroy: () => {
      resizeObserver.disconnect();
      model.destroy({ children: true });
      app.destroy(false, { children: true, texture: true, baseTexture: true });
    },
  };
}
