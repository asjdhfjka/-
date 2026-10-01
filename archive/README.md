# archive/ —— 存档：与当前脚本不配套的历史产物

这里放的都是**曾经用来出结论、但现在用仓库里的脚本复现不出来**的文件。
不删除是因为它们是当时真实跑出来的数据；挪到这里是因为它们留在项目根目录
会被误当成「当前结果」。

| 文件 | 为什么被归档 |
|---|---|
| `test_results.旧题集.json` | 用 `test_all.py` 里**当前已被注释掉**的那套题目跑出来的。它的分类含「辅导员/管理视角」，而现行题集没有这一类 —— 直接用根目录的脚本重跑，得不到这份数据。 |
| `test_tree.旧题集.png` | 上表数据的树状图，随之失效。 |
| `compare_result.旧格式.json` | 旧版对照脚本的产物。旧脚本的「原始版调用次数」是写死的字符串而非实测值，结论不可复现。 |
| `review_rules.孤儿文件.json` | 全项目零引用的历史遗留规则文件，schema 与 `auto_rules.json` v2.0 不同（它是扁平的 `{"rules": [...]}`），容易误当成规则库。真正的规则库是根目录的 `auto_rules.json`。 |

## 怎么重新生成

```bash
# 1. 启动服务（默认 8001）
uvicorn main:app --host 127.0.0.1 --port 8001

# 2. 重跑评测 → 生成新的 test_results.json（带题集指纹）与 test_tree.png
python test_all.py

# 3. 前后对照（走计量代理，两版次数都是实测）
bash tools/run_compare.sh
```

新产物都会被 `tests/test_eval_artifact.py` 校验：题集指纹对不上直接失败，
杜绝「用旧数据冒充新结论」。
