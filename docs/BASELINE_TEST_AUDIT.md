# 测试基线审计

## 本轮可复现验证

在当前工作树、Python 3.12 和 `D:\blender\blender.exe` 上运行了以下聚焦套件：

```text
tests.test_semantic_plan
tests.test_blender_render_profiles
tests.test_coded_draft
tests.test_manual_annotation
tests.test_cli
tests.test_whole_story
```

结果为 **69 tests passed**。其中包含真实 Blender profile 渲染、真实 MP4 解码、人工标注器的路径/帧篡改回归，以及 coded-draft 的超时、SHA、像素和契约故障注入。

## 全量历史套件

本轮也执行过 `python -m unittest discover -q`。该命令不能作为当前实现的通过基线：用户此前清理了旧的 `runs/stage*` 调试证据，而一批历史测试把这些可变运行目录当作固定夹具，因文件不存在产生 42 failures 和 109 errors。没有为了让它们变绿而恢复、伪造或复制旧证据。

因此，论文/实验结论只引用上面的自包含聚焦套件和真实 pilot 产物；历史套件需要后续改成自包含 fixture 后再纳入全量门禁。
