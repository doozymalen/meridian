"""Meridian 스티칭 엔진."""

import os

# numpy 가 쓰는 OpenBLAS 는 부를 때마다 자기 스레드를 따로 띄운다. 엔진은 이미 사진마다
# 스레드를 나눠 돌리므로, 여러 스레드가 동시에 행렬 곱(v @ rot)을 부르면 윈도우에서
# OpenBLAS 가 접근 위반으로 엔진을 통째로 죽였다 (미리보기 워핑 중 rays_to_pixels).
# 한 갈래로 묶어 둔다. numpy 를 처음 불러오기 전에 정해야 효과가 있어 여기에 둔다.
for _var in ("OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")
