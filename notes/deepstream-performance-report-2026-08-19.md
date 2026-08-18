# Bao cao hieu nang DeepStream Safety

## Pham vi

- Runtime: `deepstream_safety`
- Dashboard: `http://localhost:8080/dashboard.html`
- HLS: `http://localhost:8888/safety_bbox/index.m3u8`
- Input: mock video face-recognition, chay `mock_loop: false`
- GPU: NVIDIA GeForce RTX 3050 Laptop GPU

## So lieu thuc te

Metrics duoc doc truc tiep tu WSL, `nvidia-smi`, PID pipeline va HLS endpoint.

- CPU host: `63.6%` tren `12 cores`
- CPU DeepStream: `760.5%`, tuong duong khoang `7.6 cores`
- RAM DeepStream: `1.6 GB`
- RAM WSL: `22.8%`, khoang `2.19 GB / 9.61 GB`
- GPU utilization: `32%`
- VRAM: `523 MB / 4096 MB`
- GPU temperature: `58 C`
- HLS playlist latency: `2.9 ms`
- HLS dang live trong luc pipeline chay

## Ket luan

Pipeline dang nang CPU nhung chua qua tai GPU. Nguyen nhan chinh la face recognition dang chay bang `CPUExecutionProvider`, da duoc xac nhan trong log:

```text
providers=['CPUExecutionProvider']
```

Person detector va smoking detector van dung TensorRT engine tren GPU. OSD va HLS khong phai blocker chinh. HLS latency thap, nen live khong bi gioi han boi output stream.

## Nguyen nhan ky thuat

1. Face detector va ArcFace duoc goi tren CPU.
2. Face recognition dang xu ly qua nhieu frame, gay tai CPU tich luy.
3. Frame phai di qua CPU memory de chay face engine, sau do quay lai GPU memory cho OSD/output.
4. Khong co co che giam tan suat recognition rieng theo tung `track_id`.

## Phuong an toi uu

### Uu tien 1: giam tan suat recognition theo track

- Person/smoking detection van chay moi frame tren GPU.
- Face recognition chi chay moi `300-500 ms` cho moi track.
- Giu lai name/score cu trong thoi gian track con hop le.
- Khi track ket thuc, xoa recognition state.

### Uu tien 2: chi chay face detector trong ROI nguoi

- Dung bbox person de cat ROI.
- Khong chay face detector tren toan bo frame.
- Dua ROI vao ArcFace sau khi loc face.

### Uu tien 3: chuyen face engine sang GPU

- Cai provider tuong thich CUDA/TensorRT trong WSL.
- Xac nhan log co `CUDAExecutionProvider` hoac `TensorrtExecutionProvider`.
- Khong coi viec cai package thanh cong la acceptance neu provider van la CPU.

## Muc tieu sau toi uu

- CPU DeepStream tu khoang `760%` xuong khoang `150-300%`.
- Giu live HLS on dinh.
- Giu ten nhan dien on dinh theo track.
- Khong them encode hoac nhanh video trung gian.

## Dashboard metrics da them

- Endpoint: `GET /api/metrics`
- Polling: moi 2 giay
- File server: `deepstream_safety/dashboard_server.py`
- UI: `deepstream_safety/dashboard.html`
- Launcher da doi tu Python static server sang dashboard metrics server trong `deepstream_safety/start.ps1`.

## Trang thai

- Da co so lieu hardware runtime thuc te.
- Da xac dinh CPU face recognition la bottleneck.
- Chua trien khai giam tan suat/ROI/GPU provider.
- Mock no-loop se tu dung khi video ket thuc; day la hanh vi chu dong, khong phai loi dashboard.
