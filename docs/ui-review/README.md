# Operator interface review

The screenshots use a disposable project and local loopback browser sessions. The ComfyUI and Blender workflow shots show deterministic fixtures so the change can be reviewed without the specialist applications or a GPU.

| View | Before | After |
| --- | --- | --- |
| Overview at 1440 px | [English](before-overview-1440.png) | [Russian](after-overview-ru-1440.png) |
| Overview at 390 px | [English](before-overview-390.png) | [Russian](after-overview-ru-390.png) · [English](after-overview-en-390.png) |
| Workflow at 390 px | | [Russian approval](after-workflow-review-ru-390.png) |
| Workflow at 1440 px | | [Russian results](after-workflow-result-ru-1440.png) |

Playwright exercised request submission, the Director conversation, task result links, approval and rejection, an empty task filter, and a localized invalid-input response. Browser checks at 320, 390, and 1440 px covered both languages, keyboard focus, table scrolling, and a disposable long Windows path. The language preference persisted after reload and across pages.

A separate approved inference step ran against a real local llama.cpp server. The worker returned the requested marker and the task page displayed it in the result. The fixture workflow decoded the PNG in the browser, used its artifact ID as input to the next step, and downloaded valid glTF 2.0 JSON. The rejected fixture produced no glTF.
