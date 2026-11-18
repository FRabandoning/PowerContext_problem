// 数据初始化脚本：把谜语 + 冷知识灌入 seekdb
// 用法: node dify/bridge/seed.js
import { getCollection, checkConnection, close, COLLECTION_NAME } from "./seekdb.js";
import { embedTexts } from "./embed.js";
import { allDocuments } from "./data.js";

async function main() {
  const db = await checkConnection();
  if (!db.connected) {
    console.error("✗ seekdb 未连接：", db.error);
    console.error("  请先启动 seekdb：docker compose up -d");
    process.exit(1);
  }
  console.log(`✓ seekdb 已连接 ${db.config.host}:${db.config.port}`);

  const col = await getCollection();
  console.log(`✓ 集合就绪: ${COLLECTION_NAME}`);

  const docs = allDocuments();
  console.log(`正在为 ${docs.length} 条数据计算向量...`);
  const embeddings = await embedTexts(docs.map((d) => d.document));

  console.log("正在写入数据...");
  await col.upsert({
    ids: docs.map((d) => d.id),
    documents: docs.map((d) => d.document),
    metadatas: docs.map((d) => d.metadata),
    embeddings,
  });

  console.log("正在刷新索引...");
  try {
    await col.refresh_index();
  } catch (e) {
    console.warn("refresh_index 跳过:", e.message);
  }

  const count = await col.count();
  console.log(`✓ 完成！集合内共 ${count} 条数据`);
  await close();
  process.exit(0);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
