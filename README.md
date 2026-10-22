# GraphRAG Lite

一个可以本地运行的、以证据为中心的 GraphRAG 原型。默认使用 **SQLite + FTS5**：实体和关系作为表存储，断言保留指向原文块的精确引文；查询时结合文本检索和多跳图遍历，以 RRF 融合、轻量重排后生成带引用的回答。SQLite 官方支持 [FTS5](https://www.sqlite.org/fts5.html) 和[递归图查询](https://www.sqlite.org/lang_with.html#queries_against_a_graph)，适合单机原型与中小规模数据。需要多人并发写入、复杂 Cypher 或图原生运维能力时，可换 Neo4j 数据适配层。

## 启动

需要 Python 3.11+，无需安装服务或 Python 第三方库。

```bash
python -m graphrag_lite.cli seed
python -m graphrag_lite.cli serve --port 8000
```

打开 <http://127.0.0.1:8000>。可试问：`星图项目的测试负责人属于哪个团队？请给出关系路径。`

CLI 查询与评测：

```bash
python -m graphrag_lite.cli ask "星图项目的测试负责人是谁？"
python -m graphrag_lite.cli eval
```

可以通过 `--db path/to/file.sqlite` 或 `GRAPHRAG_DB` 更换数据库路径。

## 文档导入与模型

导入 JSON：`python -m graphrag_lite.cli ingest document.json`。文件包含 `title`、`text`，以及可选的 `facts`。每条 fact 至少有 `subject`、`predicate`、`object`、`quote`；可加 `subject_type`、`object_type`、`confidence`。`quote` 必须是对应文本块内的连续原文，否则拒绝该断言。详见 `evals/corpus.json`。

未传 `facts` 时，关系抽取调用 OpenAI 兼容 Chat Completions API：

```bash
export LLM_API_KEY='...'
export LLM_MODEL='...'
export LLM_BASE_URL='https://api.openai.com/v1' # 可省略
```

配置模型后，回答会尝试用证据生成自然语言；未配置时返回带引用的原文摘录。模型输出的引用 ID 会校验是否属于本次检索证据，校验失败则退回原文摘录。模型抽取结果同样经过 quote 定位校验。不要将它当作强事实蕴含验证：**引文存在不代表回答必然被引文支持**。

## 流程与接口

`POST /api/ingest` 接受 `{"title":"...","text":"...","facts":[...]}`；省略 facts 时自动抽取。`POST /api/ask` 接受 `{"question":"...","mode":"auto","max_hops":3}`。`GET /api/resolutions` 查看疑似重复实体，`POST /api/resolve` 传 `{"id":"...","action":"merge|distinct"}`。`GET /api/stats` 返回统计。

1. **抽取与入库**：文本分块；每条断言验证精确摘录、置信度和实体类型；文档、块、实体、别名、断言分别持久化。
2. **消歧**：同类型精确别名自动匹配；模糊相似项进入人工队列，避免误合并。页面可合并或保留。
3. **路由**：依据问题中出现的实体与多跳词选择文本、图谱或融合路径；页面显示理由，可手动改写。
4. **检索**：文本使用 FTS5 英文词匹配和中英文词项覆盖；图谱从命中实体双向遍历 1–3 跳。两个排序以 `1/(60+rank)` 做 RRF。
5. **重排与回答**：本地重排综合问题词覆盖、RRF 与图谱支持；回答绑定本次证据块 ID，前端显示来源、路径和打分。

## 评测

`evals/questions.json` 包含单跳、多跳、三跳、纯文本、不可回答样例。`eval` 输出：

- 路由准确率：选中的路径与标注一致。
- `source_recall_at_6`：期望文档进入前六证据的比例。
- 多跳覆盖：结果中是否出现标注的最小跳数。
- 引用合法率：引用 ID 是否属于本次返回的证据。
- 拒答准确率：无证据问题是否拒答。

真实数据上线前应扩充评测集，按单跳/多跳、同名实体、冲突事实、否定句、时效性和证据不足分层；人工盲评**答案正确性、每句是否被引文支持、路径是否忠实于断言**。当前自动评测不包含独立的事实蕴含判定，也未测试大规模性能。

## 当前边界

这是单机 MVP。文本检索未使用向量嵌入；本地重排是可解释规则，并非 cross-encoder；文本块较大时跨块 quote 会被拒绝。`serve` 默认只绑定 `127.0.0.1`，没有身份认证，不宜直接暴露到公网。
