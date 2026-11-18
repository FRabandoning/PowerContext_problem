// 文本向量化：使用 Transformers.js 本地模型 all-MiniLM-L6-v2（384 维）
// 与 seekdb 集合的向量维度保持一致；无需外部 API / Key
import { pipeline, env } from "@huggingface/transformers";

const MODEL = "Xenova/all-MiniLM-L6-v2";
export const EMBED_DIMENSION = 384;

// 国内网络环境用镜像加速模型下载；可通过 HF_ENDPOINT 覆盖
env.remoteHost = process.env.HF_ENDPOINT || "https://hf-mirror.com";
env.allowLocalModels = false;

let extractor = null;

async function getExtractor() {
  if (!extractor) {
    console.log("[embed] 正在加载本地 embedding 模型（首次需下载，请稍候）...");
    extractor = await pipeline("feature-extraction", MODEL);
    console.log("[embed] ✓ 模型就绪");
  }
  return extractor;
}

// texts: string[] -> number[][]（每个向量 384 维）
export async function embedTexts(texts) {
  const ex = await getExtractor();
  const output = await ex(texts, { pooling: "mean", normalize: true });
  return output.tolist();
}
