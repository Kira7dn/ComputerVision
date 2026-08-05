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
## 📊 Tóm tắt lựa chọn
### ✅ AI Recorder: Giải pháp cơ bản, chi phí thấp
> PPE, xâm nhập chu vi, chấm công khuôn mặt, cảnh báo mất vật thể

### 🚀 LS CV: Giải pháp cấp doanh nghiệp
> Tích hợp WMS/ERP/MES/HRM, theo dõi quy trình sản xuất, đối soát tồn kho, chất lượng đóng gói, quản lý sự kiện toàn diện

---
*Cập nhật ngày: 04/08/2026*