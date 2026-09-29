"""Selectors observed on https://voters.eci.gov.in/download-eroll.

The form is a React page. Every filter is a native <select> whose id and name
match the field name. Labels are associated with for="<id>".
"""

STATE_SELECT = "select#stateCode"
YEAR_SELECT = "select#revyear"
ROLL_TYPE_SELECT = "select#roleType"
DISTRICT_SELECT = "select#district"
AC_SELECT = "select#constituency"
LANGUAGE_SELECT = "select#langCd"

CAPTCHA_INPUT = "input#captcha"
CAPTCHA_IMAGE = "img[alt='Captcha']"
# The portal spells this alt text "refrsh".
CAPTCHA_REFRESH = "img[alt='refrsh captcha']"
CAPTCHA_AUDIO = "img[alt='Read aloud captcha']"

DOWNLOAD_BUTTON = "button:has-text('Download Selected PDFs')"
SELECT_ALL = "input#selectAll"
SELECT_ALL_LABEL = "label[for='selectAll']"
PART_TABLE = "table.contenttable-eroll"
PART_ROW = "table.contenttable-eroll tbody tr"
ROW_CHECKBOX = "input[type='checkbox']"
SEARCH_INPUT = "input.search-box[placeholder='Search']"
PAGE_INDICATOR = ".pagination .control-btn2 strong"
PAGINATION_BUTTONS = ".pagination button"

API_CAPTCHA = "/api/v1/captcha-service/getCaptcha/EROLL"
API_VOICE_CAPTCHA = "/api/v1/captcha-service/generateVoiceCaptcha/"
API_ROLL_TYPES = "/api/v1/printing-publish/get-publish-eroll-type"
API_LANGUAGES = "/api/v1/printing-publish/get-ac-languages"
API_PARTS = "/api/v1/printing-publish/get-publish-part-list"
API_GENERATE = "/api/v1/printing-publish/generate-published-pdfs"
API_PUBLISHED_FILE = "/api/v1/ext-printing-publish/get-published-file"
EROLL_FILE_PREFIX = "/eroll/"
