# Electoral roll download flow

Observed on 27 September 2026 from the live page `https://voters.eci.gov.in/download-eroll` and the JavaScript chunk that renders it (`static/js/6838.fe8807b3.chunk.js`). Query ciphertext, CAPTCHA images, cookies, and authorization headers are not recorded here.

The page is a React form. State, year, roll type, district, assembly constituency, and language are native `<select>` elements. District and assembly constituency lists are filtered in the browser from data already loaded with the page. Roll types, languages, and part lists come from `https://gateway-voters.eci.gov.in`.

The portal allows at most **10 parts per CAPTCHA**. The part table shows 10 rows per page, and Select All applies to the current page only.

## 1. Open the page

Frontend action: browser opens `/download-eroll`.

Request: `GET https://voters.eci.gov.in/download-eroll`

Response: HTML shell. The form is rendered by the React bundle.

Next UI state: State dropdown, Year of Revision (2026, 2025, 2024), CAPTCHA, and **Download Selected PDFs**. Roll type, district, constituency, and language are not on the page yet.

A second request loads a fresh CAPTCHA:

`GET https://gateway-voters.eci.gov.in/api/v1/captcha-service/getCaptcha/EROLL`

The JSON body has a `data` field. The page turns that into `img[alt="Captcha"]` (observed 200 by 80 pixels, `src` is a data URL). It then requests audio:

`GET https://gateway-voters.eci.gov.in/api/v1/captcha-service/generateVoiceCaptcha/{captchaId}`

Response content type: `audio/wav`. The control is `img[alt="Read aloud captcha"]`. Refresh is `img[alt="refrsh captcha"]` (the portal spells it that way). The text field is `input#captcha` with `maxlength="6"`.

This tool does not read, store, refresh, or solve that image.

## 2. State

Frontend action: choose `select#stateCode`.

The options are already in the page (36 states and union territories, codes such as `S05` for Goa). Choosing a state does **not** call `/api/v1/common/states`.

Next UI state: the page asks for roll types for the selected state and the current year.

## 3. Year

Frontend action: `select#revyear`.

The options are the current year and the two previous years, built in the browser. On this date the selected value is `2026`. The tool leaves it alone when it is already 2026.

Changing the year asks for roll types again.

## 4. Roll type

Frontend action: state and year are set.

Request: `GET https://gateway-voters.eci.gov.in/api/v1/printing-publish/get-publish-eroll-type`

The query values are transformed in the browser before the request is sent. They are not the plain state code and year. The response JSON is readable.

Response shape, confirmed for Goa / 2026:

- `status`: `Success`
- `statusCode`: `200`
- `message`: `Publish Eroll Data fetched Successfully!`
- `payload`: list of roll types

Each item includes `id`, `displayName`, `rollTypeRefId`, `pdfGenType`, `revisionNo`, and `byElecAcList`.

Goa returned 6 roll types:

| id | display name |
| --- | --- |
| S05-2026-SUPP-4 | Supplement-4 2026 |
| S05-2026-SUPP-3 | Supplement-3 2026 |
| S05-2026-SUPP-2 | Supplement-2 2026 |
| S05-2026-BY-FIR-2 | Bye Election Final Roll-Revision2 2026 |
| S05-2026-FIR | SIR FinalRoll - 2026 |
| S05-2026-DR | SIR DraftRoll - 2026 |

Next UI state: `select#roleType` appears. The page selects the first roll type itself.

## 5. District and assembly constituency

For a normal roll type, district and constituency dropdowns are filled in the browser. No district or constituency API ran when Goa was selected.

`select#district` is optional. Leaving it on "Select District" lists every assembly constituency. Choosing a district filters that list. Goa's districts were Kushavati (`S0503`), North Goa (`S0501`), and South Goa (`S0502`). With no district selected, Goa showed 40 constituencies, the first being `1 - Mandrem`.

For a bye-election roll type the id contains `-BY-` as its third segment (`S05-2026-BY-FIR-2`). The district `<select>` is removed. Constituencies come from that roll type's `byElecAcList`. For Goa that list was only `21 - Ponda`.

Frontend action: choose `select#constituency`.

Two requests then run together.

### Languages

`POST https://gateway-voters.eci.gov.in/api/v1/printing-publish/get-ac-languages`

Response payload is a map of language code to name. Mandrem returned:

```json
{ "ENG": "ENGLISH", "MAR": "MARATHI" }
```

Next UI state: `select#langCd` selects the first language.

### Parts

`POST https://gateway-voters.eci.gov.in/api/v1/printing-publish/get-publish-part-list`

Response payload is the full part list, not just the visible page. Mandrem / Supplement-4 returned 48 parts. Each item includes `partNumber`, `partName`, `partId`, `districtCd`, `acNumber`, and `stateCd`. The first part was part 1, "Government Primary School, Tiracol", district `S0501`.

Next UI state: `table.contenttable-eroll` with:

- header checkbox `input#selectAll` and label "Select All"
- column "Part No and Part Name"
- a search box, `input[placeholder="Search"]`
- pager buttons `<<`, `<`, `>`, `>>`
- 10 rows on the first page

Changing language updates `select#langCd` only. The part list request does not run again.

## 6. CAPTCHA check

The image is created when the page loads, from `getCaptcha/EROLL`. Validation is not a separate call on this form. The typed value is sent with the download request. The page blocks the click locally if the box is empty ("Please Enter Valid Captcha") or if more than 10 parts are selected ("Maximum 10 Parts are allow once at a time").

The tool stops here, focuses the browser, and waits. The operator types the CAPTCHA and clicks **Download Selected PDFs**. The tool does not refresh the image. After a successful submission the page itself clears the box and loads a new CAPTCHA for the next batch.

## 7. Download

Frontend action: click **Download Selected PDFs** with 1 to 10 parts selected.

Request: `POST https://gateway-voters.eci.gov.in/api/v1/printing-publish/generate-published-pdfs`

The JSON body carries the state, constituency, selected part numbers, district code, language, roll id, and the CAPTCHA text plus CAPTCHA id. An empty or rejected CAPTCHA comes back as a JSON message and an empty `payload`. The tool treats that as a failed CAPTCHA and waits for the operator to try again. It does not guess.

Discovery did not submit a CAPTCHA, so this last request was not executed live. The page's own download handler, in the chunk loaded for `/download-eroll`, does the following after a successful response.

When the portal accepts the CAPTCHA, `payload` is a non-empty list and `statusCode` is 200. The page then delivers each item in one of two ways, decided by the response `refId`:

1. `refId === "CDN"`: the page fetches `https://voters.eci.gov.in/eroll/{path}` and saves that blob.
2. Otherwise: the page calls `GET https://gateway-vpd.eci.gov.in/api/v1/ext-printing-publish/get-published-file?fileId={id}`. The JSON `payload` is a base64 PDF and `refId` is the filename. The page builds a `data:application/pdf;base64,...` link and clicks it.

The tool saves those response bodies after the operator's CAPTCHA succeeds. It does not request roll files on its own.

## Selectors

| Control | Selector |
| --- | --- |
| State | `select#stateCode` |
| Year of Revision | `select#revyear` |
| Roll type | `select#roleType` |
| District | `select#district` |
| Assembly constituency | `select#constituency` |
| Language | `select#langCd` |
| CAPTCHA image | `img[alt="Captcha"]` |
| CAPTCHA refresh | `img[alt="refrsh captcha"]` |
| CAPTCHA audio | `img[alt="Read aloud captcha"]` |
| CAPTCHA input | `input#captcha` |
| Download | button text `Download Selected PDFs` |
| Select all | `input#selectAll` |
| Part table | `table.contenttable-eroll` |
| Part row | `table.contenttable-eroll tbody tr` |
| Search | `input.search-box[placeholder="Search"]` |
| Current page | `.pagination .control-btn2 strong` |

There is also a green **Download SIR Draft Roll for full AC** button on some states. It opens a different page. This tool does not click it.
