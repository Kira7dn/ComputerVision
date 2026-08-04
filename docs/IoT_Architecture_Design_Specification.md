# TÀI LIỆU THIẾT KẾ KIẾN TRÚC HỆ THỐNG IOT ENTERPRISE
## Kiến trúc Thu thập, Bảo mật, Nén & Xử lý Dữ liệu Telemetry (Edge-to-Cloud)

---

## 1. TỔNG QUAN KIẾN TRÚC (SYSTEM OVERVIEW)

Hệ thống được thiết kế theo tiêu chuẩn doanh nghiệp (Enterprise-grade Standard) nhằm thu thập dữ liệu telemetry định kỳ $1	ext{s}$ từ thiết bị Edge (ESP32), nén và mã hóa bất đối ứng trước khi truyền lên AWS Cloud. Hệ thống đảm bảo tính toàn vẹn, bảo mật đa tầng, tối ưu chi phí hạ tầng và khả năng mở rộng cho Data Lake.

```
┌───────────────────────────────────────────────────────────────────────────────────┐
│                                 EDGE LAYER (ESP32)                                │
│                                                                                   │
│  ┌──────────────┐     ┌──────────────────────┐     ┌───────────────────────────┐  │
│  │ Sensors (1s) │ ──► │ PSRAM Ping-Pong Buff │ ──► │ Task Process (5m Batch)   │  │
│  └──────────────┘     └──────────────────────┘     │  - Gzip (miniz)           │  │
│                                                    │  - Gen AES Key (32B)      │  │
│                                                    │  - AES-GCM Encrypt        │  │
│                                                    │  - Encrypt AES Key w/ RSA │  │
│                                                    │  - Sign w/ ATECC608A      │  │
│                                                    └─────────────┬─────────────┘  │
└─────────────────────────────────────────────────────────────────┼─────────────────┘
                                                                  │
                                                      Offline / SD Card / 4G
                                                                  │
                                                                  ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│                                    CLOUD LAYER                                    │
│                                                                                   │
│  ┌──────────────┐     ┌──────────────────────┐     ┌───────────────────────────┐  │
│  │ AWS S3 Raw   │ ──► │ AWS Lambda (Trigger) │ ──► │ AWS KMS (Decrypt AES Key) │  │
│  │ (.gz Hybrid) │     │  - Extract Metadata  │     └─────────────┬─────────────┘  │
│  └──────────────┘     │  - Verify Signature  │                   │                │
│                       │  - AES-GCM Decrypt   │ ◄─────────────────┘                │
│                       │  - Decompress Gzip   │                                    │
│                       │  - Format & Mask PII │                                    │
│                       └──────────┬───────────┘                                    │
│                                  │                                                │
│                                  ▼                                                │
│                       ┌──────────────────────┐                                    │
│                       │ AWS S3 Data Lake     │ ──► [Athena / Databricks / BI]     │
│                       │ (.parquet SSE-KMS)   │                                    │
│                       └──────────────────────┘                                    │
└───────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. KIẾN TRÚC XỬ LÝ TẠI EDGE (ESP32)

### 2.1. Quản lý Bộ nhớ & Batching Window
* **Tần suất Telemetry:** 1 record / giây ($\sim 15$ fields cảm biến).
* **Định dạng dữ liệu trên RAM:** C-Struct Binary (Packed, $\sim 52$ bytes/record) nhằm chống phân mảnh bộ nhớ (Memory Fragmentation) và tối ưu hiệu năng CPU.
* **Cơ chế Buffer:** **Ping-Pong Dual Buffering** trên PSRAM.
  * **Buffer A:** Nhận dữ liệu thu thập trực tiếp $1	ext{s}$ (Task Collector - Core 0).
  * **Buffer B:** Đang nén, đóng gói và mã hóa (Task Processor - Core 1).
* **Khung thời gian Batching:** **5 phút (300s / batch)**.
  * *Dung lượng thô:* $300 	imes 52	ext{ bytes} pprox 15.6	ext{ KB}$.
  * *Sau nén Gzip:* $pprox 1.5	ext{ KB} - 3	ext{ KB}$ (Tỷ lệ nén $\sim 80\%$).

### 2.2. Luồng đóng gói & Mã hóa Lai (Hybrid Encryption)
Đối với mỗi lô 5 phút dữ liệu:
1. **Nén dữ liệu:** Sử dụng thư viện `miniz` tích hợp trong ESP-IDF để nén chuỗi Binary/JSON thành dạng `.gz`.
2. **Sinh khóa Ephemeral AES Key:** Phần cứng ESP32 sinh ngẫu nhiên khóa `AES-256` ($32	ext{ bytes}$) và `Nonce` ($12	ext{ bytes}$).
3. **Mã hóa Payload:** Dùng bộ tăng tốc phần cứng AES của ESP32 mã hóa file `.gz` bằng `AES-256-GCM`.
4. **Mã hóa Khóa AES (Asymmetric Encryption):** Sử dụng **Cloud Public Key** (lưu sẵn trong SPI Flash của ESP32) để mã hóa chuỗi `AES Key` bằng thuật toán **RSA-OAEP-2048** hoặc **ECC**.
5. **Ký số phần cứng (Hardware Signature):** ESP32 băm SHA-256 toàn bộ gói tin và gửi Digest qua I2C đến chip bảo mật **ATECC608A** để tạo chữ ký số `ECDSA (secp256r1)`. Private Key của thiết bị nằm cố định trong Zone bảo mật phần cứng của ATECC608A và không bao giờ xuất ra RAM.

### 2.3. Đóng gói Header & Cấu trúc Binary File

```
+-------------------------------------------------------------------------+
| FIELD                   | SIZE      | DESCRIPTION                       |
+-------------------------+-----------+-----------------------------------+
| Magic Bytes             | 2 Bytes   | 0x45, 0x5A ("EZ")                 |
| Protocol Version        | 1 Byte    | 0x01                              |
| Key-ID                  | 2 Bytes   | Cloud Public Key Version (e.g. 1) |
| Device ID               | 16 Bytes  | Unique Hardware UUID / MAC        |
| Encrypted AES Key       | 256 Bytes | RSA-2048 Encrypted AES-256 Key    |
| Nonce / IV              | 12 Bytes  | AES-GCM Nonce                     |
| ECDSA Signature         | 64 Bytes  | Hardware Signature from ATECC608A |
| Payload Length          | 4 Bytes   | uint32_t (Length of Encrypted)    |
| Encrypted Payload       | N Bytes   | AES-GCM Encrypted Gzip Stream     |
+-------------------------------------------------------------------------+
```

---

## 3. LƯU TRỮ VÀ VẬN HÀNH NGOẠI TUYẾN (OFFLINE & SD CARD)

* **Cơ chế ghi SD Card:** File đã mã hóa được lưu thẳng vào thẻ nhớ FAT32 theo định dạng `/telemetry/YYYYMMDD_HHMMSS.bin`.
* **Giảm hao mòn phần cứng (Wear-leveling):**
  * Ghi file 5 phút/lần giúp giảm số lần I/O từ $86,400	ext{ lần/ngày}$ xuống còn **$288	ext{ lần/ngày}$** ($\downarrow 99.7\%$).
* **Khả năng sinh tồn khi Mất mạng (Offline Buffering):**
  * Do Cloud Public Key đã tích hợp sẵn trong Flash, ESP32 có thể nén, ký và đóng gói hoàn chỉnh mà **không cần kết nối mạng**.
  * Khi có Wi-Fi/4G trở lại, Task Network sẽ thực hiện flush dần các file từ SD Card lên AWS S3 (thông qua Presigned URL hoặc MQTT Block Transfer).

---

## 4. KIẾN TRÚC CLOUD (AWS LAMBDA & PARQUET DATA LAKE)

### 4.1. Luồng Giải mã & Xử lý (AWS Lambda Stream Processing)

```
[S3 Event: ObjectCreated] ──► [AWS Lambda]
                                   │
                                   ├── 1. Trích xuất Key-ID & Encrypted AES Key
                                   ├── 2. Gọi AWS KMS (Decrypt Encrypted AES Key -> Plaintext AES Key)
                                   ├── 3. Verify ECDSA Signature với Device Public Key
                                   ├── 4. AES-GCM Decrypt -> Raw Gzip Stream
                                   ├── 5. Decompress Gzip -> Raw Struct / JSON
                                   ├── 6. Convert & Format -> PyArrow / Pandas DataFrame
                                   └── 7. Write Parquet to S3 Data Lake (SSE-KMS)
```

### 4.2. Mã nguồn Lambda Minh họa (Python)

```python
import boto3
import zlib
import struct
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

kms_client = boto3.client('kms')

def lambda_handler(event, context):
    # 1. Đọc file từ S3 Event
    bucket = event['Records'][0]['s3']['bucket']['name']
    key = event['Records'][0]['s3']['object']['key']
    
    # 2. Extract các thành phần theo Offset
    raw_data = get_s3_bytes(bucket, key)
    key_id = raw_data[3:5]
    encrypted_aes_key = raw_data[23:279]
    nonce = raw_data[279:291]
    signature = raw_data[291:355]
    encrypted_payload = raw_data[359:]
    
    # 3. Giải mã AES Key qua AWS KMS
    kms_res = kms_client.decrypt(
        CiphertextBlob=encrypted_aes_key,
        EncryptionAlgorithm='RSAES_OAEP_SHA_256'
    )
    aes_key = kms_res['Plaintext']
    
    # 4. Giải mã Payload bằng AES-GCM
    aesgcm = AESGCM(aes_key)
    gzip_payload = aesgcm.decrypt(nonce, encrypted_payload, None)
    
    # 5. Giải nén Gzip
    raw_json = zlib.decompress(gzip_payload, 16 + zlib.MAX_WBITS)
    
    # 6. Chuyển đổi Parquet & Ghi vào S3 Data Lake
    write_to_parquet_datalake(raw_json)
```

---

## 5. MÔ HÌNH BẢO MẬT & ĐÁNH GIÁ RỦI RO (SECURITY & THREAT MODEL)

### 5.1. Phân tích Các điểm Tấn công (Threat Matrix)

| Vùng Tấn công | Kịch bản Tấn công | Cơ chế Phòng thủ & Khai tử Rủi ro |
| :--- | :--- | :--- |
| **Edge RAM / PSRAM** | Rút chip, đo bus PSRAM để đọc RAM. | **Ephemeral Key:** Lộ khóa AES chỉ ảnh hưởng 5 phút data trên RAM. Không thể đọc dữ liệu quá khứ/tương lai hay các thiết bị khác. |
| **SD Card / In-Transit** | Tháo thẻ SD hoặc bắt gói tin HTTPS. | **Hybrid Encryption:** Dữ liệu hoàn toàn mã hóa AES-256. Không có Private Key của Cloud không thể đọc được. |
| **Giả mạo Dữ liệu** | Chèn gói tin giả vào S3. | **Hardware ECDSA:** Chữ ký số tạo từ ATECC608A. Chặn 100% dữ liệu không nguồn gốc. |
| **S3 Storage / Data Lake** | Ransomware / Lộ API Key S3. | **SSE-KMS & S3 Object Lock (WORM):** Chống ghi đè/xóa trong 90 ngày. Bật Versioning và Block Public Access. |
| **Lộ Credential Dev** | Dev lỡ push API Key lên Github. | **IAM Least Privilege:** Role Lambda chỉ có quyền `PutObject` folder định sẵn và `kms:Decrypt`. Hạn chế bán kính thiệt hại (Blast Radius). |

### 5.2. Quản lý Vòng đời Khóa (Key Lifecycle & Rotation)
* **Ephemeral AES Key:** Sinh mới $100\%$ ngẫu nhiên mỗi 5 phút.
* **Cloud Public Key Rotation:** Đổi Key thông qua bản cập nhật **OTA (Over-The-Air)**. Hệ thống Cloud hỗ trợ Multi-Key dựa trên `Key-ID` trong Header giúp chuyển đổi mượt mà (Zero-Downtime).

---

## 6. ƯỚC TÍNH CHI PHÍ HẠ TẦNG CLOUD (AWS COST ESTIMATION)

Tính toán cho **100 Thiết bị ESP32** hoạt động liên tục $24/7$:

* **Số lượng File sinh ra:** $100 	ext{ devices} 	imes 288 	ext{ files/day} 	imes 30 	ext{ days} = 864,000 	ext{ requests/tháng}$.
* **AWS Lambda Cost:** Dưới $1,000,000 	ext{ free requests/month} \implies \mathbf{\$0.00}$.
* **AWS S3 Storage Cost:** 100 thiết bị $\sim 7.7 	ext{ GB/tháng}$ (ở dạng Parquet nén) $\implies \mathbf{<\$0.20 / tháng}$.
* **AWS KMS Cost:** 864k lượt giải mã KMS $\implies \mathbf{pprox \$2.50 / tháng}$.

$$	ext{Tổng Chi phí Cloud cho 100 Thiết bị} pprox \mathbf{\$2.70 	ext{ USD / tháng}}$$

---

## 7. KẾT LUẬN

Kiến trúc **ESP32 (ATECC608A + Hybrid Gzip) $ightarrow$ S3 $ightarrow$ AWS Lambda (KMS) $ightarrow$ Parquet Data Lake** đáp ứng tối đa tiêu chuẩn an toàn bảo mật, đạt được độ tin cậy cao về tính toàn vẹn dữ liệu, đồng thời mang lại hiệu quả kinh tế vượt trội trong vận hành thực tế.
