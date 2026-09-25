VISION_PROMPT = (
    "Describe this image in one concise paragraph. Mention only things that are "
    "actually visible. Do not invent location, brand, equipment model, people or "
    "other facts."
)

STRUCTURED_PROMPT = """Analyze this stock photograph and return ONLY valid JSON compatible with the existing AIAnalysis schema.

Describe only what is visible or directly inferable from the image. Do not invent brands, people, equipment, location, or text. Do not claim the image is AI-generated without evidence. Keep confidence between 0 and 1. Include all AIAnalysis fields, using empty strings/lists and the default people object when not applicable."""

STOCK_ANALYSIS_PROMPT = """Analyze this stock photograph for Stocker metadata. Return ONLY valid JSON compatible with the existing AIAnalysis schema.

Populate these fields when supported by visible evidence: description, title, keywords, categories, subject, commercial_context, technical_subjects, people, brands, logos, text_visible, editorial_risk, confidence. Only describe visible or directly inferable facts. Do not invent brands, people, equipment, location, or text. Do not claim the image is AI-generated without evidence. Keep confidence between 0 and 1."""

MULTI_IMAGE_PROMPT = "Compare these images. Describe the visible differences in subject, composition, technical characteristics and commercial context. Do not invent facts that are not visible."

TOOL_PROMPT = "A stock image has just been analyzed. Choose the appropriate next action: retrieve existing asset information before saving metadata, or run QC if the image needs a quality check. Use the available tool and provide only the arguments required by its schema."

