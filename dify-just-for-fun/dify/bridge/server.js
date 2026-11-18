// seekdb 桥接服务 —— 供 Dify 通过 HTTP Request 节点调用的检索 API
// 职责：把 Dify 的查询文本交给 seekdb 做「向量 + 全文」混合检索，返回相关谜语/冷知识
import express from "express";
import cors from "cors";
import { getCollection, checkConnection, close, COLLECTION_NAME, VECTOR_DIMENSION } from "./seekdb.js";
import { embedTexts } from "./embed.js";
import { allDocuments } from "./data.js";

const app = express();
const PORT = process.env.BRIDGE_PORT || 3001;

app.use(cors());
app.use(express.json({ limit: "10mb" }));

// 健康检查 + seekdb 连接状态
app.get("/api/health", async (req, res) => {
  const db = await checkConnection();
  res.json({ service: "dify-seekdb-bridge", version: "1.0.0", database: db });
});

// 混合检索
// body: { query: string, category?: "riddle"|"fact"|"all", n?: number }
app.post("/api/retrieve", async (req, res) => {
  const { query, category = "all", n = 5 } = req.body || {};
  if (!query || typeof query !== "string") {
    return res.status(400).json({ error: "query 不能为空" });
  }

  const k = Math.min(Math.max(1, parseInt(n, 10) || 5), 20);
  // 元数据过滤：按类型（谜语/冷知识）筛选
  const where = category === "all" ? undefined : { type: category };

  try {
    const col = await getCollection();
    const t0 = Date.now();

    // 本地计算查询向量（all-MiniLM-L6-v2, 384 维）
    const queryEmbeddings = await embedTexts([query]);

    const results = await col.hybridSearch({
      query: {
        whereDocument: { $contains: query },
        where,
        nResults: k,
      },
      knn: {
        queryEmbeddings,
        where,
        nResults: k,
      },
      rank: { rrf: {} }, // RRF 融合向量与全文两路结果
      nResults: k,
    });

    const elapsed = Date.now() - t0;

    const ids = results.ids?.[0] || [];
    const documents = results.documents?.[0] || [];
    const distances = results.distances?.[0] || [];
    const metadatas = results.metadatas?.[0] || [];

    const formatted = ids.map((id, i) => ({
      id,
      document: documents[i] || "",
      distance: distances[i] ?? null,
      metadata: metadatas[i] || {},
    }));

    res.json({
      success: true,
      query,
      category,
      nResults: k,
      searchMs: elapsed,
      mode: "hybrid",
      results: formatted,
    });
  } catch (e) {
    console.error("检索失败:", e);
    res.status(500).json({ error: e.message });
  }
});

// 灌入种子数据（幂等：按 id 覆盖）
app.post("/api/seed", async (req, res) => {
  try {
    const col = await getCollection();
    const docs = allDocuments();

    const embeddings = await embedTexts(docs.map((d) => d.document));

    await col.upsert({
      ids: docs.map((d) => d.id),
      documents: docs.map((d) => d.document),
      metadatas: docs.map((d) => d.metadata),
      embeddings,
    });

    // 刷新索引，确保新数据立即可检索
    try {
      await col.refresh_index();
    } catch (e) {
      console.warn("refresh_index 跳过:", e.message);
    }

    const count = await col.count();
    res.json({ success: true, seeded: docs.length, total: count });
  } catch (e) {
    console.error("灌入失败:", e);
    res.status(500).json({ error: e.message });
  }
});

// 清空集合
app.post("/api/reset", async (req, res) => {
  try {
    const { getClient } = await import("./seekdb.js");
    const c = await getClient();
    await c.deleteCollection(COLLECTION_NAME);
    // 重建（内部缓存置空）
    const mod = await import("./seekdb.js");
    mod.collection = null;
    await getCollection();
    res.json({ success: true, message: "集合已重置" });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// 统计
app.get("/api/stats", async (req, res) => {
  try {
    const col = await getCollection();
    const count = await col.count();
    res.json({ success: true, collection: COLLECTION_NAME, dimension: VECTOR_DIMENSION, count });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.listen(PORT, () => {
  console.log("=".repeat(56));
  console.log("  seekdb 桥接服务（Dify 专用）");
  console.log(`  - 检索: POST http://localhost:${PORT}/api/retrieve`);
  console.log(`  - 灌数据: POST http://localhost:${PORT}/api/seed`);
  console.log(`  - 健康检查: GET http://localhost:${PORT}/api/health`);
  console.log("=".repeat(56));
  checkConnection().then((db) => {
    if (db.connected) {
      console.log(`[seekdb] ✓ 已连接 ${db.config.host}:${db.config.port}`);
    } else {
      console.log(`[seekdb] ✗ 未连接: ${db.error}`);
      console.log(`[seekdb] → ${db.hint}`);
    }
  });
});

process.on("SIGINT", async () => {
  await close();
  process.exit(0);
});
