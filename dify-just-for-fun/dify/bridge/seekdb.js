// seekdb 客户端单例（Dify 桥接服务专用）
// 向量由 embed.js 手动计算（all-MiniLM-L6-v2 384 维），因此显式禁用默认 embedding
import { SeekdbClient } from "seekdb";
import mysql from "mysql2/promise";

const CONFIG = {
  host: process.env.SEEKDB_HOST || "127.0.0.1",
  port: parseInt(process.env.SEEKDB_PORT || "2881", 10),
  database: process.env.SEEKDB_DATABASE || "stepfun_kb",
  user: process.env.SEEKDB_USER || "root",
  password: process.env.SEEKDB_PASSWORD || "",
};

export const VECTOR_DIMENSION = 384; // all-MiniLM-L6-v2
export const COLLECTION_NAME = "stepfun_riddles";

let client = null;
let collection = null;

async function ensureDatabase() {
  // seekdb 服务器初始不存在 stepfun_kb 库，需先建库再以该库连接
  const conn = await mysql.createConnection({
    host: CONFIG.host,
    port: CONFIG.port,
    user: CONFIG.user,
    password: CONFIG.password,
  });
  try {
    await conn.query(`CREATE DATABASE IF NOT EXISTS \`${CONFIG.database}\``);
  } finally {
    await conn.end();
  }
}

export async function getClient() {
  if (client) return client;
  await ensureDatabase();
  client = new SeekdbClient(CONFIG);
  return client;
}

export async function getCollection() {
  if (collection) return collection;
  const c = await getClient();
  // fulltextConfig 使用 ngram 分词，适合中文关键词/子串匹配
  // embeddingFunction: null 表示由调用方手动传入向量（embed.js），不加载默认 embedding
  collection = await c.getOrCreateCollection({
    name: COLLECTION_NAME,
    configuration: {
      hnsw: { dimension: VECTOR_DIMENSION, distance: "cosine" },
      fulltextConfig: { analyzer: "ngram" },
    },
    embeddingFunction: null,
  });
  return collection;
}

export async function checkConnection() {
  try {
    await getClient();
    // 注：v2 集合格式下 listCollections() 可能返回空，改用 getCollection() 确认真实集合
    const col = await getCollection();
    const count = await col.count();
    return {
      connected: true,
      config: { host: CONFIG.host, port: CONFIG.port, database: CONFIG.database },
      collections: [COLLECTION_NAME],
      collection: { name: COLLECTION_NAME, count },
      vectorDimension: VECTOR_DIMENSION,
    };
  } catch (e) {
    return {
      connected: false,
      error: e.message,
      hint: "请确保 seekdb server 已在 127.0.0.1:2881 启动（docker compose up -d）",
      config: { host: CONFIG.host, port: CONFIG.port, database: CONFIG.database },
    };
  }
}

export async function close() {
  if (client) {
    try {
      await client.close();
    } catch (e) {
      // ignore
    }
    client = null;
    collection = null;
  }
}
