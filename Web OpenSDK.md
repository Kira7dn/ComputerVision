# Web Plugin-Free Player Development Guide

---

## 1. Revision History

| Version | Updates |
| :--- | :--- |
| **1.3.1** | Add Open ARC Video Play and Optimize document structure. |
| **1.3.2** | Modify the export way of `PlaySDKInterface.js` |
| **1.3.3** | Adjust SDK file structure and optimize SDK guidance instructions |
| **1.3.4** | Add Talkback Alarm Example |

---

## 2. Overview

This document mainly explains how to integrate the Web plugin-free SDK from the following aspects:

| Aspect | Description |
| :--- | :--- |
| **API Call Flow** | API call flow for plugin-free playback stream acquisition |
| **SDK Architecture** | SDK package file structure and usage after extraction |
| **SDK Usage Guide** | SDK interface functions, methods, parameters and return values |

---

## 3. Process of Calling Service Interface from DoLynk Developer to Use the Player Without Plug-ins

* **Interface for live video stream pulling:** `/open-api/api-iot/device/createDeviceStreamUrl`
* **Interface for video clip search:** `/open-api/api-iot/device/queryLocalRecords`

For more information, please go to **DoLynk Developer > Document > API Reference > API List > IoT Interfaces > Video Play**.

---

## 4. SDK Architecture

Package contains: `sdk` and `demo-vue` folders.

### 4.1 sdk

The `WasmLib` and `360ARTagAsserts` folders in the `sdk` directory are plugin-free player library resources. When integrating into your project, place them in the project’s static resources folder (preferably in the static resources root directory), as `videoPlay-js.js` references these `WasmLib` and `360ARTagAsserts` resources to ensure proper resource loading.

If not placed in the resources root directory, you need to manually modify the resource reference paths in `videoPlay-js.js`. The `videoPlayer` folder should be directly integrated into your project, and import the `videoPlay-js` file when using it. For specific usage, please refer to the demo examples.

### 4.2 demo-node

`demo-node` is a demo example referenced by a single HTML file (`index.html` is the demo file), and it needs to be run with a Node.js service.

* **Command for project installation:** `npm install`
* **Command for project startup:** `node index.js`

**Playback Code Explanation:**
* The HTML part sets up the playback container, and the JavaScript code imports `videoPlay-js.js`.
* After the DOM is loaded, the initialization method is executed, and an array of playback container IDs is passed to the `Player` constructor.
* After obtaining the stream address, execute the `play` method for live/recorded video playback.

### 4.3 demo-vue

`demo-vue` is a Vue 3 demo example.

* **Command for project installation:** `npm install`
* **Command for project startup:** `npm run dev`

**Development Environment Configuration:**
After selecting the corresponding environment’s `API_TARGET_URL` in the `config/constant.ts` file, run `npm run dev` to start the demo application.

**Project structure:**
* `README.md` explains how to use the player without plug-ins, including the parameters, methods, callback events, usage, and FAQ.
* The component of the player without plug-ins is located at `src/components/videoPlayer`. When you are developing a project, introduce `playWasm` and `videoPlay-js.js` to your project. In order to ensure that the resources can be loaded properly, you also need to introduce `WasmLib` and `360ARTagAsserts` under `public` to the root resource directory of the project.
* The IOT player demo `Index.vue` is located at `src/views/iotPlayerDemo`. The ARC player demo `Index.vue` is located at `src/views/arcPlayerDemo`. The demo shows functions like playing live or recorded videos without plug-ins, sound control, live intercom, video speed control, intelligence frame, real-time recording, and local snapshots for videos.

---

## 5. SDK Usage Guide

### 5.1 Overview

Supports plugin-free playback of H264/H265 encoded live streams and recordings on web browsers.

* **Decoding Mode:** Multi-threaded decoding is used by default (Google Chrome ≥ 91, Firefox ≥ 97, Edge ≥ 91). Single-threaded decoding is used if these conditions are not met.
* **Hardware Decoding:** H264 encoding uses hardware decoding by default. H265 encoded videos use hardware decoding by default when Google Chrome ≥ 104. When the device is set to smart encoding mode, only software decoding is supported.

### 5.2 API

```javascript
import Player from './videoPlayer/videoPlay-js.js'

player = new Player(['player'], {
  playError,
  playFileOver,
  talkStart,
})

await player.init()

player.play('player', {
  streamURL: '',
  deviceId: '',
  channelId: '0',
  bitStream: 0,
  isLive: true,
})

```

#### Constructor: `new Player(ids, callbacks)`

| Field Name | Type | Description | Required |
| --- | --- | --- | --- |
| `ids` | `Array<string>` | Collection of playback window container IDs | Yes |
| `callbacks` | `Object` | Collection of registered callback events | No |

#### Methods

| Name | Description | Parameter Description |
| --- | --- | --- |
| `init` | Initialization method | None |
| `play` | Live/recording playback | `(id: string, options: PlayOptions) => void` |
| `talk` | Live talk | `(id: string, options: PlayOptions) => void` |
| `download` | Recording download | `(id: string, options: PlayOptions, fileName: string) => void` |
| `record` | Real-time recording | `(id: string, filename: string) => void` |
| `screenshot` | Video screenshot | `(id: string) => void` |
| `close` | Close video playback | `(id: string) => void` |
| `setAudioVolume` | Set video volume | `(volume: Number) => void`, range: 0–1 |
| `setTalkVolume` | Set talk volume | `(volume: Number) => void`, range: 0–1 |
| `pause` | Pause video | `(id: string) => void` |
| `start` | Resume video playback | `(id: string) => void` |
| `playFF` | Recording speed control | `(speed: Number) => void`, speed values: `0.25`, `0.5`, `1`, `2`, `4`, `8`, `16` |
| `openIVS` | Enable smart frame | `(id: string) => void` |
| `closeIVS` | Disable smart frame | `(id: string) => void` |
| `downloadPause` | Pause recording download | `(id: string) => void` |
| `downloadStart` | Resume recording download | `(id: string) => void` |
| `downloadClose` | Close recording download | `(id: string) => void` |

#### PlayOptions Properties

| Field Name | Type | Description | Required |
| --- | --- | --- | --- |
| `isLive` | `boolean` | Whether it’s live stream | Yes |
| `streamURL` | `string` | Playback stream URL | Yes |
| `deviceId` | `string` | Device ID | Yes |
| `channelId` | `string` | Channel ID | Yes |
| `bitStream` | `number` | Stream type, 0: Main stream, 1: Sub stream | Yes |
| `npt` | `string` | Recording offset | No |
| `volume` | `number` | Video/talk volume setting 0–1, defaults to 1 | No |
| `isSandardPack` | `boolean` | Whether it’s standard stream packaging (true: RTSP standard packaging; false: Dahua frame packaging, default false) | No |

#### Callback Events

| Name | Description | Parameter Description |
| --- | --- | --- |
| `playError` | Triggered when error occurs during playback | `(id, err) => void` |
| `talkError` | Triggered when error occurs during talk | `(id, err) => void` |
| `recordError` | Triggered when error occurs during real-time recording | `(id, err) => void` |
| `captureCallback` | Callback function after screenshot completion, returns image blob data | `(id, blob) => void` |
| `downloadError` | Triggered when error occurs during recording download | `(id, err) => void` |
| `videoDownloadDuration` | Callback function for recording download duration | `(time) => void` |
| `downloadComplete` | Triggered when recording download completes | `(id) => void` |
| `downloadProgressUpdate` | Returns current recording clip timestamp and UTC time (unit: s) | `(id, msg) => void` |
| `runtimeInitializedCallBack` | Callback function when runtime initialization completes | `() => void` |

#### Error Codes

| Code | Description |
| --- | --- |
| **101** | Video media source error |
| **201** | Audio format not supported |
| **202** | Error establishing WebSocket connection |
| **203** | KMS unavailable |
| **205** | Device busy |
| **206** | Device disconnected from talk |
| **404** | RTSP/RTSV not found |
| **408** | Short connection timeout |
| **457** | Invalid range |
| **500** | Service error |
| **503** | Service unavailable |
| **504** | Talk service unavailable |
| **999** | No RTSP/RTSV response |

---

### 5.3 Notes for Using the Player

When you are using the player without plug-ins, you need to configure the cross-domain embedding strategy for the service.

Take Nginx configuration for example. You need to add 2 lines of code to the Nginx configuration file:

```nginx
add_header Cross-Origin-Embedder-Policy "credentialless";
add_header Cross-Origin-Opener-Policy "same-origin";

```

> **Lưu ý:** Nếu bạn cấu hình chiến lược này, các trang bên thứ ba được nhúng có thể bị ảnh hưởng. Nếu bạn không cấu hình chiến lược này, trình phát không cần plugin sẽ tự động chuyển sang chế độ hiệu năng thấp (low-performance mode).
