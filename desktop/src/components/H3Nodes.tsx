import { Handle, Position, type NodeProps } from "@xyflow/react";
import {
  Aperture,
  AudioLines,
  FileOutput,
  Images,
  Sparkles,
  WandSparkles,
} from "lucide-react";
import { QUALITY_PRESETS, type CanvasNodeData, type H3CanvasNode } from "../types";

function NodeFrame({
  data,
  icon,
  children,
  output = true,
  input = true,
}: {
  data: CanvasNodeData;
  icon: React.ReactNode;
  children: React.ReactNode;
  output?: boolean;
  input?: boolean;
}) {
  return (
    <article className={`h3-node h3-node--${data.status ?? "idle"}`}>
      {input && <Handle type="target" position={Position.Left} />}
      <header className="h3-node__header">
        <span className="h3-node__icon">{icon}</span>
        <strong>{data.label}</strong>
        <i aria-label={data.status} />
      </header>
      <div className="h3-node__body">{children}</div>
      {output && <Handle type="source" position={Position.Right} />}
    </article>
  );
}

export function MediaStackNode({ data }: NodeProps<H3CanvasNode>) {
  const count = data.assetIds?.length ?? 0;
  return (
    <NodeFrame data={data} icon={<Images size={16} />} input={false}>
      <div className="node-visual node-visual--stack">
        {[0, 1, 2].map((item) => <span key={item} />)}
        <b>{count || "＋"}</b>
      </div>
      <p>{count ? `${count} 张参考图 · 自动编号` : "从素材库拖入，最多 9 张"}</p>
    </NodeFrame>
  );
}

export function PromptNode({ data }: NodeProps<H3CanvasNode>) {
  const prompt = String(data.prompt ?? "");
  return (
    <NodeFrame data={data} icon={<AudioLines size={16} />} input={false}>
      <p className="node-prompt">
        {prompt || "描述动作、镜头和声音…"}
      </p>
      <span className="node-meta">{prompt.length} 字</span>
    </NodeFrame>
  );
}

export function GenerateNode({ data }: NodeProps<H3CanvasNode>) {
  const preset = QUALITY_PRESETS[data.presetId ?? "daily"];
  return (
    <NodeFrame data={data} icon={<Aperture size={16} />}>
      <div className="node-model">H3 <small>ref2va 自动</small></div>
      <dl className="node-specs">
        <div><dt>{preset.width}×{preset.height}</dt><dd>{preset.label}</dd></div>
        <div><dt>{preset.steps} 步</dt><dd>beta · 24fps</dd></div>
      </dl>
    </NodeFrame>
  );
}

export function UpscaleNode({ data }: NodeProps<H3CanvasNode>) {
  return (
    <NodeFrame data={data} icon={<WandSparkles size={16} />}>
      <div className={`node-switch ${data.enabled ? "is-on" : ""}`}>
        <span />{data.enabled ? "SeedVR2 已启用" : "保持原始画面"}
      </div>
      <p>增强到 1080×1920，保留原片</p>
    </NodeFrame>
  );
}

export function ExportNode({ data }: NodeProps<H3CanvasNode>) {
  return (
    <NodeFrame data={data} icon={<FileOutput size={16} />} output={false}>
      <div className="node-output"><Sparkles size={18} /> H.264 10-bit</div>
      <p>CRF 14 · 音频随片输出</p>
    </NodeFrame>
  );
}

export const H3_NODE_TYPES = {
  mediaStack: MediaStackNode,
  prompt: PromptNode,
  generate: GenerateNode,
  upscale: UpscaleNode,
  export: ExportNode,
};
