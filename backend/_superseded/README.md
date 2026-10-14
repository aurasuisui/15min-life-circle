# _superseded

这里存放**被 docs/SOLUTION.md §4.2 实测否定**的实现，仅作记录，不参与构建。

- `isochrone.py` —— 极坐标连点 + Chaikin 平滑的等时圈。
  合成场景实测显示：内接星形多边形会切掉一切凹角，**系统性低估面积 5%~11%**，
  而凹角恰恰对应河湾、封闭小区等最需要识别的区域。
  替代实现见 `app/core/isochrone.py`（射线采样 → IDW 耗时场 → marching squares）。
- `test_isochrone.py` —— 上者的单元测试。
