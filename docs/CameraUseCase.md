# 📊 Bảng so sánh Giải pháp Camera AI tại Nhà máy
## So sánh AI Recorder (Hikvision/Dahua) và Hệ thống LS CV (Link Strategy)

---
### 📌 Giải thích các viết tắt ngành:
- `WMS`: Hệ thống Quản lý Kho
- `DO`: Lệnh Xuất/Nhập Kho
- `ERP`: Hệ thống Quản lý Doanh nghiệp
- `MES`: Hệ thống Quản lý Sản xuất
- `HRM`: Hệ thống Quản lý Nhân sự
- `OEE`: Hiệu suất Thiết bị
- `SLA`: Thỏa thuận Mức Dịch vụ

---
### 🎯 Hướng dẫn icon trạng thái (áp dụng cho cả 2 cột):
- ✅ **Tốt**: Hỗ trợ sẵn có, hoạt động tốt
- ❌ **Không hỗ trợ**: Chức năng không có
- 🔺 **Hạn chế**: Có nhưng giới hạn (VD: chỉ trên dòng camera đắt tiền / khó đếm hàng đặc thù / dễ báo giả)

---
## 1. 🏭 Kho & Logistics (Điểm mạnh chủ yếu)
| # | Use Case | AI Recorder | LS CV |
|---|---|---|---|
| 1 | 📦 Bốc xếp / Di chuyển hàng | ✅ Báo động chuyển động | ✅ Đối chiếu WMS: báo pallet di chuyển không có DO |
| 2 | 🚶 Bám đuôi qua cửa kho | ❌ Chỉ đếm người qua cổng | ✅ Đối chiếu Access Control: 3 người qua nhưng 1 quẹt thẻ |
| 3 | 🚗 Đọc biển số (ANPR) | ✅ Mở barrier theo danh sách trắng | ✅ Check ERP/Lịch xe: đúng xe, đúng giờ mới mở cổng |
| 4 | 📦❓ Vật thể bị mất/bỏ quên | ✅ Báo động hú còi | ✅ Tạo Incident, lưu clip 30s, truy xuất người quẹt thẻ |
| 5 | 🔢 Đếm vật tư lớn (Pallet) | 🔺 Khó đếm hàng đặc thù | ✅ Custom model đếm pallet, báo chênh lệch Dashboard |
| 6 | 🔢 Đếm sản phẩm / Sản lượng | ✅ Đếm hộp/kiện qua vạch | ✅ Đối soát MES theo giờ/ca, báo lệch ngay |
| 7 | 🚛 Kiểm tra hàng xếp lên xe | ❌ Chỉ ghi hình | ✅ Đối chiếu DO, báo thiếu/thừa trước khi xe rời bến |
| 8 | 🔄 Xe chạy ngược chiều / quay đầu | 🔺 Báo chuyển động lạ | ✅ Gắn biển số + thời gian, lập biên bản tự động |

---
## 2. 🏭 Quy trình sản xuất
| # | Use Case | AI Recorder | LS CV |
|---|---|---|---|
| 9 | 👷 Tuân thủ quy trình (SOP) | ❌ Không làm được | ✅ Theo dõi thao tác, báo lỗi sai thứ tự |
| 10 | ⏱️ Đo thời gian lưu trạm | ❌ Không làm được | ✅ Bấm giờ vào/ra trạm, gửi MES tính bottleneck |
| 11 | 💡 Đọc trạng thái máy móc | ❌ Không làm được | ✅ Đọc đèn tháp + OCR màn hình lấy data OEE |
| 12 | ⚙️ Máy chạy không tải (Idle) | ❌ Không làm được | ✅ Báo nếu máy chạy nhưng không có công nhân |
| 13 | 🔩 Kiểm tra linh kiện lắp ráp | ❌ Không làm được | ✅ Đếm bộ phận, báo thiếu linh kiện trước chuyển trạm |

---
## 3. 🛡️ An toàn lao động (EHS)
| # | Use Case | AI Recorder | LS CV |
|---|---|---|---|
| 14 | 🚫 Vi phạm vùng cấm | 🔺 Còi hú ồn, báo giả | ✅ Báo theo ngữ cảnh: trừ tổ bảo trì đang có lịch |
| 15 | 🦺 Đồ bảo hộ (PPE) | ✅ Rất tốt, sẵn trên firmware | ✅ Ghi vào HRM tính KPI an toàn, kèm clip bằng chứng |
| 16 | 🔥 Khói, lửa, té ngã | ✅ Tốt trên camera Thermal/AcuSense | ✅ Workflow sơ tán, API báo hệ thống chuông báo cháy |
| 17 | ⚠️ Va chạm xe nâng – người | ❌ Chỉ phát hiện vật thể | ✅ Đo khoảng cách, cảnh báo sớm xe nâng gần người |
| 18 | ⚡ Khu vực hàn (Hot Work) | ❌ Không hỗ trợ | ✅ Kiểm tra kính/che chắn đúng quy định |
| 19 | 🧯 Thiết bị PCCC | ❌ Không hỗ trợ | ✅ Phát hiện bình mất/che/hết hạn thẻ kiểm định |

---
## 4. 🔒 An ninh vật chất
| # | Use Case | AI Recorder | LS CV |
|---|---|---|---|
| 20 | 🚧 Xâm nhập chu vi | ✅ Rất xuất sắc, lọc nhiễu | ✅ Push Zalo/Telegram cho Đội trưởng bảo vệ |
| 21 | 🧑 Nhận diện khuôn mặt (Chấm công) | ✅ Nhận diện nhanh, lưu log | ✅ Gắn ca làm việc, báo nghi vấn ở lại quá giờ |
| 22 | 👀 Lảng vảng (Loitering) | ✅ Báo đứng quá lâu | ✅ Audit Trail + phân loại rủi ro theo vị trí |

---
## 5. 📦 Tối ưu hóa kho & tồn kho
| # | Use Case | AI Recorder | LS CV |
|---|---|---|---|
| 23 | 🚛 Hiệu suất ụ bốc hàng (Dock) | ❌ Chỉ thấy có xe hay không | ✅ Đo Turnaround Time, so SLA đánh giá KPI |
| 24 | 🔍 Đọc mã Seal / QR / Mã vạch | ❌ Chỉ đọc biển số | ✅ Đọc Seal/QR, đối chiếu Packing List ERP trước xuất |
| 25 | 🗄️ Đối soát lấp đầy kệ hàng | ❌ Không hiểu cấu trúc kho | ✅ Quét kệ định kỳ, cross-check WMS báo sai lệch tồn |
| 26 | 🚢 Container trống trước khi xếp | ❌ Không hỗ trợ | ✅ Quét sàn container, báo còn hàng/sót rác |

---
## 6. 🧪 Kiểm soát chất lượng quy trình sản xuất
| # | Use Case | AI Recorder | LS CV |
|---|---|---|---|
| 27 | ⚙️ Ùn ứ băng chuyền (Conveyor Jam) | ❌ Không phân tích dòng chảy | ✅ Theo mật độ hàng, báo đứng im/dồn ứ >3 phút |
| 28 | 📱 Sử dụng điện thoại / Xao nhãng | 🔺 Camera AI đắt tiền, dễ báo giả | ✅ Pose tracking, tạo Record vi phạm vào HRM |
| 29 | 📦 Kiểm tra đóng gói cuối cùng | ❌ Không phải chức năng NVR | ✅ Check niêm phong/co màng trước khi lên xe |
| 30 | 🔍 Ngoại quan bề mặt (Defect) | ❌ Không hỗ trợ | ✅ Phát hiện trầy xước, biến dạng, lỗi màu |
| 31 | 🏷️ Vị trí dán tem / nhãn | ❌ Không hỗ trợ | ✅ Kiểm tra tem lệch, sai vị trí, sai mã |

---
## 7. 🛡️ EHS nâng cao
| # | Use Case | AI Recorder | LS CV |
|---|---|---|---|
| 32 | 🧹 Tuân thủ 5S / Vật cản lối thoát | 🔺 Abandoned Object báo giả | ✅ Chụp định kỳ, gửi ảnh vào Zalo Group vệ sinh |
| 33 | 💧 Tràn đổ hóa chất / Nước | ❌ Không hỗ trợ | ✅ Nhận diện thay đổi sắc độ/độ phản bóng sàn |
| 34 | 🚜 Xe nâng chạy quá tốc độ | ❌ Chỉ bắt tốc độ ô tô | ✅ Tính tốc độ qua Video Analytics, cảnh báo |
| 35 | 🚬 Hút thuốc / Vape sai nơi | 🔺 Nhiệt báo thuốc lá, mù với Vape | ✅ Nhận diện vật thể + hành vi đưa tay lên miệng |
| 36 | 📋 Khu vực cần giấy phép (Permit) | ❌ Không hỗ trợ | ✅ Chỉ cho vào khi giấy phép còn hiệu lực |

---
## 8. ⚙️ Vận hành hệ thống (System Ops)
| # | Use Case | AI Recorder | LS CV |
|---|---|---|---|
| 37 | 📷 Camera bị che / mất tín hiệu | ✅ Cảnh báo mất tín hiệu | ✅ Tự tạo ticket + cảnh báo hình ảnh kém chất lượng |

---
## 💰 Kịch bản thương mại 4 LPR + 4 face

Kịch bản so sánh gồm bốn làn nhận diện biển số và bốn điểm nhận diện khuôn mặt. Với
Dahua/Hikvision, cụm từ “đầu ghi AI 8 kênh” không đồng nghĩa đầu ghi tự chạy mọi model
trên tám video. Face/LPR nhiều kênh thường dùng AI nằm trong từng camera; đầu ghi nhận
metadata, lưu trữ, tìm kiếm và cảnh báo.

### Ước tính CAPEX

| Phương án | Cấu phần chính | Tổng dự kiến |
|---|---|---:|
| Dahua/Hikvision | 4 camera face AI × 8–12 triệu + 4 camera ANPR × khoảng 17,5 triệu + NVR/HDD | **114–136 triệu** |
| LS CV retrofit | Appliance thương mại 35–45 triệu + HDD/phụ kiện 5–8 triệu, dùng lại camera đạt profile | **40–53 triệu** |
| LS CV greenfield | Appliance 35–45 triệu + 8 camera phù hợp 20–40 triệu + HDD/switch 6–10 triệu | **61–95 triệu** |

Các mức trên là range planning, chưa phải báo giá bán hàng. BOM máy thử nghiệm khoảng
20 triệu không được dùng làm giá bán: appliance thương mại còn phải bao gồm chassis,
nguồn, storage, bảo hành, triển khai và biên hỗ trợ.

Nếu tận dụng camera hiện hữu, LS CV có thể thấp hơn khoảng **55–70%**. Với dự án mới,
lợi thế dự kiến khoảng **20–50%**. Lợi thế tăng thêm khi cùng server chạy face, LPR,
PPE, ngã, hút thuốc hoặc cháy/khói mà không phải thay từng camera bằng một edge AI SKU
khác.

### Điều kiện để được công bố lợi thế giá

Lợi thế trên chỉ hợp lệ sau khi profile 4+4 đạt acceptance, không được suy ra từ việc
tám stream kết nối thành công:

- Bốn LPR và bốn face camera chạy đồng thời với workload đã định nghĩa.
- Passage recall, recognition precision/recall và P95 latency đạt SLA cả ngày lẫn đêm.
- Burst tám camera không làm queue tăng vô hạn hoặc âm thầm bỏ recognition đã nhận.
- Soak 7–30 ngày không OOM, crash, mất event hoặc tăng RAM/VRAM/queue theo thời gian.
- Camera thường phải đạt Camera Profile về pixel density, shutter, WDR, góc và ánh sáng;
  server không khôi phục được khuôn mặt/biển số đã nhòe hoặc cháy sáng.
- Sản phẩm giao khách là appliance 24/7 có installer, monitoring, backup/restore,
  warranty và remote support; không phải laptop thử nghiệm.

Projection hiện tại cho thấy confidence-gated retry là cần thiết nhưng chưa đủ để phê
duyệt tám camera trên RTX 3050 4 GB. Full cascade vẫn cần benchmark và thêm headroom;
chi tiết nằm trong [Kiến trúc Camera AI B2B](architecture/Platform.md).

### Định vị cạnh tranh

- Ở một hoặc hai điểm chuyên dụng, Dahua/Hikvision vẫn có lợi thế triển khai nhanh,
  quang học được tối ưu và độ ổn định turnkey.
- Ở tám điểm hỗn hợp hoặc hệ thống camera sẵn có, LS CV có khả năng thắng về CAPEX,
  khả năng tái sử dụng camera, workflow tùy biến và số use case trên cùng compute.
- Không định vị là “camera nhận diện tốt hơn Hikvision/Dahua”; định vị là lớp AI và
  workflow đa hãng giúp doanh nghiệp tránh mua một camera AI đắt tiền cho từng điểm.

---
## 💰 Kịch bản thương mại 20 face + 2 LPR

Kịch bản phổ biến hơn cho nhà máy, tòa nhà hoặc khu công nghiệp gồm 20 điểm nhận diện
khuôn mặt và hai làn nhận diện biển số. Ba kiến trúc được so sánh:

1. Camera AI chuyên dụng tại từng điểm kết hợp đầu ghi AI của Dahua/Hikvision.
2. Camera RTSP phù hợp kết hợp một workstation GPU mạnh tại site.
3. Mười một Jetson edge node, mỗi node xử lý đúng hai camera, kết hợp một central GPU
   server full set khoảng 20 triệu đồng.

`RTX 3050` chỉ là phần cứng thử nghiệm hiện có trên laptop, không phải cấu hình đóng
gói bắt buộc của phương án thứ ba. Khi thương mại hóa, central là một máy GPU độc lập
trong ngân sách khoảng 20 triệu và không nhận 22 luồng để detect liên tục.

### Giả định chung

- Camera thường dùng cho LS CV vẫn phải đạt profile quang học của từng vị trí. Hai làn
  LPR cần shutter, WDR, tiêu cự và pixel density phù hợp; server không sửa được ảnh
  biển số đã nhòe hoặc cháy sáng.
- Chi phí camera RTSP phù hợp cho 20 face + 2 LPR được dự trù **60–100 triệu**.
- Storage dự trù khoảng 24 TB usable và UPS có chi phí **25–40 triệu**. Dung lượng thực
  tế phải chốt lại từ bitrate, số ngày retention và yêu cầu RAID.
- Dây mạng, switch PoE và nhân công chưa đưa vào tổng vì cả ba phương án đều cần; chỉ
  hạch toán riêng nếu topology làm phát sinh chênh lệch đáng kể.
- Mọi con số là range planning tại thời điểm 08/08/2026, chưa phải báo giá bán hàng.

### Phương án 1: Camera AI + đầu ghi AI

| Cấu phần | Ước tính |
|---|---:|
| 20 camera face AI, khoảng 8–12 triệu/camera | 160–240 triệu |
| 2 camera ANPR chuyên dụng | 40–100 triệu |
| NVR, storage và UPS | 35–55 triệu |
| **Tổng greenfield** | **235–395 triệu** |

Khoảng giá ANPR rộng vì camera kiểm soát cổng và camera giao thông tốc độ cao là hai
phân khúc khác nhau. Ví dụ, Hikvision iDS-TCM403-BI đang được niêm yết tới 49,9 triệu
đồng/camera. Giá face AI 8–12 triệu là range planning theo các SKU cùng phân khúc,
không phải giá công khai đã xác nhận riêng cho iDS-2SH6B6G0-IZS.

Phương án này có lợi thế turnkey, quang học chuyên dụng, bảo hành theo hệ sinh thái và
ít phải vận hành phần mềm. Đổi lại, CAPEX tăng gần tuyến tính theo số điểm AI, phụ
thuộc vendor và khó dùng cùng phần cứng để bổ sung các model riêng như ngã, hút thuốc,
bạo động hoặc cháy/khói.

Tham khảo:

- [Hikvision iDS-TCM403-BI ANPR](https://hikvision.vn/san-pham/camera-ip/camera-than-tru-4mp-nhan-dien-bien-so-xe-hikvision-ids-tcm403-bi.html)
- [Hikvision iDS-2SH6B6G0-IZS face AI](https://hikvision.vn/san-pham/camera-ip/camera-ai-dem-nguoi-nhan-dien-khuon-mat-hikvision-ids-2sh6b6g0-izs.html)

### Phương án 2: Một workstation GPU mạnh

Cấu hình planning gồm GPU 16 GB VRAM cấp RTX 5070 Ti, CPU 12 core, RAM 64 GB, NVMe
2 TB, storage giám sát và UPS.

| Cấu phần | Greenfield | Retrofit, dùng lại camera đạt profile |
|---|---:|---:|
| 22 camera | 60–100 triệu | 0 |
| Workstation compute | 65–80 triệu | 65–80 triệu |
| Storage và UPS | 25–40 triệu | 25–40 triệu |
| **Tổng** | **150–220 triệu** | **90–120 triệu** |

Đây là phương án có CAPEX thấp nhất nếu 22 camera nằm tại cùng site và mạng LAN đủ
tốt. Vận hành cũng đơn giản nhất vì chỉ có một máy compute cần cập nhật và giám sát.
Tuy nhiên, workload không được chạy brute-force trên mọi frame. Điều kiện kỹ thuật là
detector nhẹ, tracking, quality gate, top-K evidence, dedupe và confidence-gated retry
phải giảm số lần gọi face recognition và OCR.

### Phương án 3: 11 Jetson edge + central GPU 20 triệu

Mỗi Jetson phục vụ đúng hai camera. Edge chịu trách nhiệm decode RTSP, human/vehicle
detection, tracking, quality scoring và chọn top-K evidence. Edge có thể tính thêm
face embedding hoặc plate detection khi benchmark cho thấy đủ headroom. Central chỉ
nhận observation, embedding và evidence đã chọn để identity matching, OCR có điều
kiện, aggregate event, render canonical media và gửi notification.

```text
22 camera
  -> 11 Jetson, mỗi node 2 camera
       -> observation + top-K evidence/embedding
            -> central GPU server khoảng 20 triệu
                 -> Event SOT -> media -> notification
```

NVIDIA công bố Jetson Orin Nano Super Developer Kit 8 GB đạt tới 67 sparse TOPS,
băng thông bộ nhớ 102 GB/s và giá chuẩn 249 USD. Đây là lựa chọn phù hợp hơn Pi 5 4 GB
+ AI HAT+ 13 TOPS cho pipeline video vì có CUDA, TensorRT và video decoder của NVIDIA.

| Cấu phần | Mua gần giá chuẩn | Giá bán lẻ trong nước cao |
|---|---:|---:|
| 11 Jetson hoàn chỉnh | 85–105 triệu | 120–145 triệu |
| Central GPU server full set | 20 triệu | 20 triệu |
| Storage và UPS | 25–40 triệu | 25–40 triệu |
| 22 camera greenfield | 60–100 triệu | 60–100 triệu |
| **Tổng greenfield** | **190–265 triệu** | **225–305 triệu** |
| **Tổng retrofit** | **130–165 triệu** | **165–205 triệu** |

Mức 249 USD là giá developer kit của NVIDIA, không phải BOM production bảo đảm mua
được tại Việt Nam. Khi đóng sản phẩm B2B, cần cộng enclosure, NVMe, nguồn, tản nhiệt,
watchdog và dự phòng thay thế. Với số lượng lớn, production module cùng carrier board
công nghiệp phải được báo giá lại thay vì lấy developer kit làm giá cam kết.

Tham khảo:

- [NVIDIA Jetson Orin Nano Super Developer Kit](https://www.nvidia.com/en-eu/autonomous-machines/embedded-systems/jetson-orin/nano-super-developer-kit/)
- [Raspberry Pi AI HAT+](https://www.raspberrypi.com/products/ai-hat/)

### So sánh tổng hợp 20 + 2

| Kiến trúc | Greenfield | Retrofit | Khi nên chọn |
|---|---:|---:|---|
| Camera AI + NVR AI | **235–395 triệu** | Không có lợi thế nếu phải thay camera | Turnkey, ít tùy biến, ưu tiên hệ sinh thái vendor |
| Workstation GPU mạnh | **150–220 triệu** | **90–120 triệu** | Một site, LAN tốt, ưu tiên CAPEX và vận hành đơn giản |
| 11 Jetson + central 20 triệu | **190–305 triệu** | **130–205 triệu** | Nhiều khu vực/chi nhánh, uplink yếu, cần fault domain hai camera |

### Kết luận kiến trúc và chi phí

- **Một site tập trung:** workstation mạnh là phương án kinh tế nhất và có ít điểm
  vận hành nhất.
- **Nhiều khu vực phân tán:** 11 Jetson không mặc nhiên rẻ hơn workstation, nhưng giảm
  video truyền về trung tâm, cô lập lỗi theo cụm hai camera và cho phép mở rộng dần.
- **Turnkey theo vendor:** camera AI + NVR là phương án đắt nhất nhưng giảm trách nhiệm
  phát triển và tích hợp phần cứng ở giai đoạn đầu.
- Không bán phương án Jetson bằng luận điểm “rẻ nhất”. Giá trị của nó là kiến trúc
  phân tán, store-and-forward, giảm băng thông và khả năng tiếp tục xử lý cục bộ khi
  central hoặc uplink tạm thời không sẵn sàng.
- Trước khi chốt BOM, phải benchmark hai stream thật trên một Jetson và replay burst
  từ cả 11 node vào central 20 triệu. Giá thành chỉ có ý nghĩa khi passage recall,
  face precision/recall, P95 latency, nhiệt độ và queue đều đạt SLA.

---
## 📊 Tóm tắt lựa chọn
### ✅ AI Recorder + camera AI: Giải pháp turnkey theo từng điểm
> Phù hợp 1–2 điểm chuyên dụng; đầu ghi rẻ nhưng chi phí tăng theo số camera AI face/ANPR

### 🚀 LS CV workstation: CAPEX tốt nhất tại một site
> Phù hợp hệ thống tập trung: AI đa tính năng, tích hợp WMS/ERP/MES/HRM và quản lý sự kiện thống nhất

### 🌐 LS CV Jetson edge: Kiến trúc phân tán
> Phù hợp nhiều khu vực hoặc uplink hạn chế; central 20 triệu chỉ xử lý candidate/evidence, không detect liên tục 22 stream

---
*Cập nhật ngày: 08/08/2026*
