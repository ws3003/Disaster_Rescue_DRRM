\# Disaster\_Rescue\_DRRM 项目说明





\## 项目名称



Disaster\_Rescue\_DRRM



灾害场景智能救援排序系统





\## 当前版本



V0.1





\## 项目目标



基于 YOLO26n + MediaPipe + DRRM 的灾害场景人员状态识别与救援优先级排序系统。





\## 已完成模块



当前 V0.1 已完成：



1\. YOLO26n 灾害目标检测



2\. 人员类别过滤



3\. MediaPipe人体姿态估计



4\. 五类人体状态识别



\- Standing

\- Sitting

\- Lying

\- Fall

\- Help-Waving





5\. DRRM单帧风险评分



6\. 救援优先级排序



7\. 图片可视化输出





\## 当前目录结构



src/



detector.py

YOLO检测



pose.py

人体姿态



state.py

状态识别



risk.py

DRRM风险计算



ranking.py

救援排序



main.py

完整流程





\## 当前开发阶段



AIC第一阶段展示版本。





\## 开发原则



1\. 不随意重构已有架构。



2\. 修改代码前说明原因。



3\. 每完成一个模块必须验证运行。



4\. 保留论文设计对应关系。



5\. 不提前开发树莓派部署。





\## 后续规划





V0.1.5:



展示增强



\- 多图片测试

\- JSON结果输出

\- Demo优化





V0.2:



动态时序版本



\- 视频输入

\- TSF时序状态机

\- Static Lying

\- 动态DRRM





V1.0:



边缘部署



\- Raspberry Pi 5

\- 模型优化

\- 实时推理

