---
id: exec
name: 决策参考
version: 1.0.0
summary: 写给要做决定的人：发生了什么、对我们有什么影响、有哪几种应对、风险在哪
reader: 管理者和业务负责人，时间少、要的是影响和选项，不关心技术细节
length: 800-1100 字
material:
  list: "5"
  max_docs: 8
  prefer: [primary, update, disputed]
  facts: verified_first
references:
  min_sources: 3
  min_primary: 1
  max_per_source: 2
  prefer: [primary, media]
  dated: true
structure:
  - id: bottom
    heading: 结论先行
    goal: 三句话以内：发生了什么、影响有多大、建议关注到什么程度，带出处
    words: 120
  - id: facts
    heading: 需要知道的事实
    goal: 用列表列 3-5 条已经确认的事实，每条一句话带出处，不写技术细节
    words: 250
  - id: impact
    heading: 可能的影响
    goal: 以【分析】开头，分“短期/中长期”或按受影响的对象说明影响，并写出依据是哪几条事实
    words: 220
  - id: options
    heading: 可以怎么应对
    goal: 以【分析】开头，给 2-3 个可选做法，每个写清前提和代价；不替读者拍板
    words: 200
  - id: risks
    heading: 判断可能出错的地方
    goal: 哪些信息还不确定、哪些说法只有单一来源，出现什么信号说明判断要改
    words: 150
style:
  - 先说结论，每段第一句就是要点
  - 不用技术术语；必须用时，用一句话说清它对业务意味着什么
  - 事实和判断严格分开：判断段落以【分析】开头
images:
  cover: 1
  body_min: 1
  prefer: [chart, table]
  placement: 影响或对比数据的图放在“可能的影响”之后
---
写作要求：

- 读者可能只看“结论先行”，这一段必须自成一体、不会被误读。
- 建议要给选项，不要给命令；每个选项都说清需要什么前提。
