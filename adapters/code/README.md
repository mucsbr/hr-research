# 远端程序适配器协议与 QA 源码

这里保存源码，主 API/Worker 和本机测试不导入或执行它。`boss_reference_v1.py`、`boss_reference_v2.py` 是明确的受控改版样例，**不是已升级的真实上游仓库版本**。上游提交、依赖锁、wheel SHA 和实际运行镜像由代码包服务独立固定和核验。

v1 读取 `zpData.jobList` 和 `.job-sec-text`。v2 保留旧列表兼容，额外读取 `zpData.vacanciesV2` 下的 `position.key="job:<id>"`、`position.title`、`company.name/id`；编号由代码拆解，并返回原字符串的切片证据。v2 通过 BeautifulSoup 解析 `.jd-v2` 或旧正文区域，并支持已观察的下一页链接。它需要明确锁定 BeautifulSoup 及其传递依赖，不能依靠基础镜像中未登记的环境包。

Runner 固定入口为 `run(operation, payload)`，严格核对协议、平台与操作后分派到 `parse_list(request)`、`parse_detail(request)`、`navigation(request)`，返回严格 JSON。调用前必须由第一方 `prepare_program_input()` 生成 `ProgramContext`；**只有 `context.request` 能发往远端**。上下文里的原导航令牌、绑定和公司身份留在第一方内存中，不是模型输入或候选参数。

请求包含：

```json
{
  "protocol": "adapter-program/1",
  "input_id": "input_<canonical-content-sha256>",
  "operation": "parse_list",
  "kind": "boss",
  "documents": [{"id": "json", "type": "json", "content": {"zpData": {"jobList": []}}}],
  "links": [{"id": "link_<sha256>", "purpose": "next", "label": "下一页"}]
}
```

HTML 资料的 document id/type 为 `dom/html`。详情输入另有已核验岗位的 `job: {id,title}`。`input_id` 按 protocol/kind/operation/脱敏 documents/公开 links/job 的规范化 JSON 计算；同内容同操作稳定。link ID 是标准化已观察 URL 与用途的 SHA，完整 URL 仅保留在第一方。执行器另将任务、来源和代次纳入 Runner 幂等键，不能单凭 input_id 授予跨任务权限。

列表输出示例（数字和名称仅为 QA）：

```json
{
  "protocol": "adapter-program/1",
  "input_id": "与请求一致",
  "operation": "parse_list",
  "records": [{
    "row": {"document_id": "json", "pointer": "/zpData/jobList/0"},
    "fields": {
      "id": {"value": "job1", "evidence": {"pointer": "/encryptJobId"}},
      "title": {"value": "工程师", "evidence": {"pointer": "/jobName"}},
      "company_id": {"value": "acme", "evidence": {"pointer": "/encryptBrandId"}}
    }
  }],
  "navigation": []
}
```

字段 pointer 相对于该行，支持 RFC6901 的 `~0/~1`；行必须是原 JSON 数组中的单个对象。脱敏不会压缩数组。DOM 行使用 `row: {document_id:"dom",selector:".job-card:nth-of-type(1)"}`；字段证据为相对该行的 selector，可用空 selector 引用行本身，允许白名单属性或文本。`start/end` 是原标量/文本/属性的 Unicode 字符切片，不允许任意表达式。代码可负责结构搜索、复杂解析和切片定位；第一方重新读取证据并核验字段，候选不能通过自报 passed 放行。

列表字段白名单为 id/title/company/company_id/salary/location/experience/education/department/function/url_area；可另给 `locations: [Claim,...]`。每个声明均需原文证据。公司归属使用第一方 `same_company`；URL使用平台固定模板或与已核验ID一致的观察链接，候选没有 URL、observed_at、coverage 或权限字段。空列表返回 `recognized=false`，不能据此声明零招聘。

详情输出用 `row` 与 `fields.description/requirements` 替代 records，正文必须引用原文。第一方返回原岗位的副本，仅更新这两个正文字段。

导航只允许：

- `{type:"follow",link_id:"..."}`：候选不能提供 URL。岗位链接还必须属于该次列表已核验公司岗位，或当前已核验详情岗位；独立导航阶段通常只能跟随下一页。
- `{type:"scroll",pixels:1..1500}`。
- `{type:"wait",milliseconds:1..1500}`。

每次输出最多6个导航动作，滚动累计最多3000px、等待累计最多3000ms。点击、任意URL、POST、headers和代码动作均拒绝。执行器实际操作前仍必须执行 DNS/跳转/平台范围/账号锁/任务取消/代次检查，逐动作消耗整个任务预算；此模块不联网也不操作浏览器。

第一方返回类型：`validate_list_output()` 返回 `ValidatedProgramList`，含 records、navigation、raw_count、rejected_company_count、recognized，以及仅供内存导航的 jobs/job_navigations；`validate_detail_output()` 返回 `(JobDocument, navigation)`；`validate_navigation()` 返回 `ProgramNavigation` 列表。`raw_count` 仅是候选提交且经过证据检查的记录数量，不是平台总数。

输入上限1MB、JSON深度20/节点20000、HTML节点10000；输出上限300KB/100条岗位。当前支持有证据的数组记录和可唯一定位的DOM岗位区域，不声明能证明任意未知网页结构的完整性。所有实际候选执行和依赖安装须在专用远端隔离环境验证，本机单测只验证受控输出，不运行这里的源码。

`qa_cases_v1.json` 包含6个独立规定的可信用例和其原始QA资料：旧列表/旧正文/无下一页滚动为回归；复合编号新列表/新版正文区域/已观察下一页为改版用例。expected 未通过运行候选计算，已逐项经过第一方证据验证。修改候选不能修改这一测试真值。

联合验证入口为 `scripts/verify_adapter_program_remote.py`，仅允许授权Linux节点运行。它经真实 `AdapterRunnerClient` 上传源码、解析锁并请求Broker，保存签名回执，再用第一方验证器和冻结expected核对；不会在driver进程import候选。目标为旧代码6例、新代码6例，以及新代码空依赖负对照。新代码锁定 `beautifulsoup4==4.13.5`，通过真实 `select_one/get_text` 行为与相同源码无依赖时失败来证明依赖生效，不用自报版本号代替运行证据。Driver保存原幂等key；未知提交只查询原job，不自动新建。实际结果另存验证文档，不把脚本存在当作已经通过，也不将组件验证冒充API发布/恢复完整闭环。
