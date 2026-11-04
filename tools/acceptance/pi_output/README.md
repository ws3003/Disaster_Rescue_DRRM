# Pi 输出目录约定

用户指定后续新文件统一放在 /home/pi/testtest，旧目录不移动、不删除。

- 脚本：/home/pi/testtest/scripts/e7e8-output-v1/run_sensor_validation.py
- 新结果：/home/pi/testtest/results/b_sensor_video_TEST_<随机后缀>/
- 每轮配置、测试库、原始读数日志、视频及 result.json 都由现有 helper.perform 使用新结果目录生成。
- 未来备份/安装包/额外日志也放在 testtest 下；本启动脚本不创建或移动旧备份，不修改项目中的 helper。

本脚本校验已安装 helper 的 LF 归一化 SHA256，然后复用其 perform/assess；仅控制输出目录，不更改业务、测试门限、失败条件或硬件流程。已发生的 reader_or_runner_error 不会被隐藏或放宽。

Pi 调用方式（已有实机结果见本次交付报告）：

```bash
python3 -B /home/pi/testtest/scripts/e7e8-output-v1/run_sensor_validation.py --repo /home/pi/Disaster_Rescue_DRRM_V4.4_B --scenario level2
```

首次传入 --output-root /home/pi/testtest 也可显式指定；默认是当前用户主目录下 testtest。结果始终在其 results 子目录。若 helper 来源变化会停止，需要先核对版本，不覆盖现有代码。

仓库入口：tools/acceptance/pi_output/run_sensor_validation.py。Pi 可继续使用已经传入 testtest 的同版脚本；无需覆盖现场源码。

软件自测：python -B -m unittest discover -s tools/acceptance/pi_output -p test_output_location.py -v
