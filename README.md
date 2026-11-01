# ServiceNow User Guide Reviewer

A Streamlit web app that reviews ServiceNow user guides (PDF or DOCX, in English, Arabic, or both) and reports spelling, grammar, formatting, terminology, and clarity issues.

It has three review engines:

- **Built-in checks (always on).** Free rule-based checks that run on the server.
- **Free AI review (optional, works online).** An open-source model (Qwen 3.8 27B or GPT-OSS 120B) on Groq's free tier. See [Free AI review](#free-ai-review-open-source-works-online).
- **ChatGPT review (optional, local only).** Uses your ChatGPT plan when the app runs on your computer. See [ChatGPT review](#chatgpt-review-optional-runs-on-your-computer).

Uploaded files are processed in memory, never saved to disk, and never modified.

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit interface |
| `reviewer.py` | Extracts text and formatting from PDF/DOCX, runs the review, exports the report |
| `checks.py` | The rule-based review engine |
| `chatgpt_auth.py` | Official Sign in with ChatGPT flow (sign-in, token validation, refresh, sign-out) |
| `ai_review.py` | AI review (free open-source model or ChatGPT), safeguards, merging with built-in findings |
| `servicenow_terminology.json` | Verified ServiceNow terminology with official sources |
| `custom_words.txt` | General technical words the English dictionary is missing (checkbox, dropdown…) |
| `requirements.txt` | Dependencies |
| `.streamlit/config.toml` | Upload size limit and Streamlit settings |

## Run locally

Requires Python 3.10 or newer.

```bash
cd servicenow-guide-reviewer
pip install -r requirements.txt
streamlit run app.py
```

Open http://localhost:8501, upload a file, and click **Review Document**.

## Deploy free on Streamlit Community Cloud

1. Push the `servicenow-guide-reviewer` folder to a GitHub repository (public or private). `.gitignore` keeps any old `.env` file out of the repository.
2. Go to https://share.streamlit.io, sign in with GitHub, and click **Create app**.
3. Choose the repository and branch, and set **Main file path** to `app.py` (or `servicenow-guide-reviewer/app.py` if the folder is not the repository root).
4. Click **Deploy**. You don't need any secrets or environment variables.

The online version uses the built-in checks only. The ChatGPT panel is replaced by a short note, because OpenAI doesn't support Sign in with ChatGPT plan usage for hosted apps (see below).

Resource use is small: the whole app uses about 150 MB of RAM (well under the ~1 GB limit), the install has no AI models or large downloads, and a typical guide is reviewed in under a second.

## How the review works

1. **Extraction.** DOCX: each paragraph's text, style, heading level, font, size, bold, and list type, plus tables. PDF (PyMuPDF): each text block with its font and size, by page. Every finding includes its page or paragraph number, and DOCX findings also name the section.
2. **Checks.** `checks.py` runs dictionary lookups and explicit rules (see the table below).
3. **Report.** You get a summary, a filterable table (Arabic is displayed right-to-left), and Markdown or CSV downloads. Every finding has a severity and one of these statuses:
   - **Confirmed**: a rule that is reliably an error (e.g. `the the`, `Service now`, a numbering gap 1, 2, 4).
   - **Suggestion**: an optional improvement or style consistency issue.
   - **Needs Manual Review**: the tool can't be sure, e.g. an unfamiliar term or an Arabic word missing from the dictionary.

## What is supported and what is limited

The engine uses dictionaries and rules, not a large language model. It doesn't understand meaning, so it reports only what a rule can detect.

| Area | Fully supported | Limited or not supported |
|---|---|---|
| **English spelling** | Dictionary check (pyspellchecker) with suggestions. ServiceNow terms, acronyms, CamelCase names, and capitalized mid-sentence words (names, UI labels) are never flagged. | Real-word errors such as "form" instead of "from" are not detected. |
| **English grammar & punctuation** | Repeated words, a/an (including acronyms like "an SLA"), lowercase sentence starts, lowercase "i", spaces before punctuation, missing spaces after punctuation, doubled punctuation. | No full grammar parsing: subject-verb agreement, tense, and word order are not checked. |
| **Arabic spelling** | Known hamza errors (الى ← إلى, إختيار ← اختيار, ارسال ← إرسال, …), including with attached و/ف/ب/ل/ك. Typos not found in a 140k-word Arabic dictionary (marked *Needs Manual Review* because Arabic word forms vary a lot). Frequency-based hints for ة/ه, ى/ي, and hamza. | Arabic grammar (agreement, case endings) is not checked. |
| **Arabic punctuation** | Latin `, ? ;` in Arabic text (suggests `، ؟ ؛`), spacing around punctuation, mixed 0-9 and ٠-٩ digits. | |
| **Formatting (DOCX)** | Headings at the same level with different font, size, or bold; skipped heading levels; bold body text that should be a heading; inconsistent heading capitalization or end punctuation; body font/size outliers; repeated empty paragraphs; double spaces; numbering gaps, duplicates, and wrong starts; mixed number formats (1. vs 1)); mixed bullet symbols; mixed manual and automatic numbering. | Automatic list numbers aren't visible to the reader, so their sequence isn't checked. Fonts inherited from styles count as "default". |
| **Formatting (PDF)** | Body font/size outliers, near-identical heading sizes (e.g. 15 pt vs 16 pt), typed numbering and bullets, spacing. | PDFs don't store heading styles, so headings are estimated from font size and PDF formatting findings are suggestions. Scanned pages are detected and skipped (no OCR). |
| **ServiceNow terminology** | Wrong spelling or capitalization of official names (ServiceNow, Service Portal, Flow Designer, CMDB, ITSM…), misspelled multi-word terms (e.g. "Assignment Gorup"), inconsistent capitalization of terms and labels, unfamiliar acronyms and product-style names flagged for manual review. Sources come from `servicenow_terminology.json`. | Descriptions of ServiceNow functionality are **not** fact-checked; that requires understanding meaning. Unfamiliar terms are never assumed wrong. |
| **Content quality** | Repeated sentences, long sentences (>35 words), vague phrases (etc., and/or, as needed…), the same button used with different verbs (click vs. press Submit). | Unclear instructions, missing steps, and contradictions between sections can't be judged without an AI model. |
| **English ↔ Arabic** | When the Arabic part is a translation of similar length: ServiceNow terms that appear in only one language, and different numbers of steps or headings. | It doesn't check that the translation means the same thing. |

**Why there is no AI model.** Small open-source models that fit within Streamlit Community Cloud's limits (about 1 GB RAM) are not reliable enough to review grammar in English and Arabic, and they tend to invent problems. This tool puts accuracy first, so it reports only what explicit rules can detect.

## Customizing

- **Add ServiceNow terms** to `servicenow_terminology.json` only after verifying them in official documentation (https://www.servicenow.com/docs/). Put the documentation URL in `source`, and set `"enforce_case": true` for official product names whose exact capitalization should be checked. The current `source` fields point to product areas; replace them with exact article URLs as you verify each term.
- **Stop a word being flagged as misspelled** by adding it to `custom_words.txt` (lowercase, one per line).
- **Add Arabic spelling rules** to `ARABIC_FIXED` in `checks.py`.

## ChatGPT review (optional, runs on your computer)

### Why it is local only

ChatGPT review uses OpenAI's official **Sign in with ChatGPT** "ChatGPT plan usage" feature (https://developers.openai.com/siwc/token-sharing-open-source), which is currently in preview. According to OpenAI's documentation:

- It is documented for **open-source and locally hosted apps**. For remotely hosted apps, OpenAI asks developers to complete an interest form (https://openai.com/form/sign-in-with-chatgpt-interest/).
- After sign-in, OpenAI must redirect back to `http://127.0.0.1:<port>/auth/callback` on the computer running the browser. A hosted server such as Streamlit Community Cloud can't receive that redirect.
- Your ChatGPT login must be kept in a protected local file and replaced on every refresh. Streamlit Community Cloud can't do this reliably.

For these reasons the ChatGPT panel only appears when the app is opened at `localhost` on your own machine. The hosted version keeps working with the built-in checks.

### Sign-in steps

1. Have an eligible ChatGPT plan. Plus usage counts toward the five-hour limit shared by all apps; Pro has no five-hour limit.
2. Start the app locally (`streamlit run app.py`) and open http://localhost:8501.
3. Click **Continue with ChatGPT**. Your browser opens OpenAI's sign-in page.
4. Sign in, choose your workspace, and approve the app ("ServiceNow User Guide Reviewer") using your ChatGPT plan. **Keep the "use your ChatGPT plan" permission on**, or ChatGPT review stays disabled.
5. The browser shows "Signed in" and the app updates. Click **Got it** on the "You're using your ChatGPT plan" message.
6. Choose a model, tick **I authorize sending this document's text to OpenAI**, upload a document, and click **Review Document**.

Use **Manage usage** to see or limit this app's usage in ChatGPT settings (https://chatgpt.com/settings/usage). **Sign out** revokes the session at OpenAI and deletes the tokens from your computer.

### How it works and what is protected

- **Official flow only.** It uses OAuth with PKCE, state, and nonce, plus dynamic client registration and a permanent random ID for your computer. The ID token is verified against OpenAI's published signing keys (issuer, audience, expiry, nonce), and the `chatgpt.tokens.use.direct` permission is checked before any AI request. No browser cookies, unofficial endpoints, or workarounds are used.
- **Token storage.** Tokens are kept outside the project folder, in `%APPDATA%\servicenow-guide-reviewer\` on Windows or `~/.config/servicenow-guide-reviewer/` on macOS/Linux. They are written atomically with owner-only permissions and are never logged, shown, or committed.
- **Documents.** Text is sent to OpenAI only after you tick the authorization box for that review. Requests use `store: false`, and the app never saves the document.
- **Review.** ChatGPT receives the whole document (with locations and formatting), the terminology file, and the built-in findings, and is told to report only new issues. It can search the web only on `servicenow.com`. Then the app:
  - removes findings the built-in checks already reported;
  - removes any non-ServiceNow source link;
  - marks a ChatGPT terminology finding as **Needs Manual Review** if it has no official source.
  Each finding shows **Found by: Rules** or **ChatGPT**.
- **Errors.** If you reach a usage limit, are not eligible, or OpenAI is unavailable, the app says so (with a **Manage usage** link for limits) and still shows the built-in results.

### If the app doesn't connect

- **"permission to use your ChatGPT plan was not granted"**: click **Continue with ChatGPT** again and allow plan usage.
- **"not available for this account or workspace"**: your plan or workspace isn't eligible for this preview.
- **Lost or leaked credentials**: disconnect the app in ChatGPT Settings, then sign in again.

## Free AI review (open-source, works online)

The app can use a free, open-source model hosted on **Groq's free plan**. Open-source means the model's weights are public; Groq runs it for free so the Streamlit server doesn't need a GPU. Two models are offered:
- **Qwen 3.8 27B** (default): strong in Arabic and English.
- **OpenAI's open-weight GPT-OSS 120B.**

### Set it up (once, about 3 minutes)

1. Create a free account at https://console.groq.com. No credit card is needed for the free plan.
2. Open **API Keys → Create API Key**, name it `servicenow-reviewer`, and copy it. It's shown only once.
3. In Streamlit Community Cloud, open your app → **⋮ → Settings → Secrets**, paste the line below, and click **Save**:
   ```toml
   GROQ_API_KEY = "paste-your-key-here"
   ```
4. The app restarts and shows the **Free AI review** panel.

To use it when running locally, create `.streamlit/secrets.toml` with the same line. `.gitignore` keeps that file out of GitHub. Never put the key in the code.

### Free plan limits

Groq's free plan allows about **8,000 tokens per minute and 200,000 per day** for these models, so:
- Long guides are sent in parts, and the app waits automatically between parts. A 20-page guide takes a few minutes.
- Roughly **10–15 guides per day** fit in the free allowance, shared by everyone using your app. When it runs out, the app says so and still shows the built-in results.
- Document text is sent to Groq for review, and only after the user ticks the authorization box. See Groq's data policy at https://console.groq.com/docs/your-data.

### How mistakes are kept out

No AI is error-free. The app applies these safeguards to every AI finding (free AI and ChatGPT):

1. **The quote must exist.** If the AI quotes text that isn't in the document, the finding is discarded. If the quote exists but at a different location, the location is corrected.
2. **Verified terms are protected.** A spelling finding on a term from `servicenow_terminology.json` or `custom_words.txt` is discarded.
3. **Official sources only.** Source links that aren't on servicenow.com are removed, and a terminology finding without an official source is downgraded to **Needs Manual Review**.
4. **No duplicates or empty findings.** Findings already reported by the built-in checks are removed, as are "corrections" that change nothing and malformed findings.
5. **Labelled.** Each AI finding is marked **Found by: AI (model name)** so you can tell it apart from rule results.

Review AI suggestions before applying them, especially **Needs Manual Review** items.

