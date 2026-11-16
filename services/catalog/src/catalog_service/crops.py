"""The crop reference list (id, name, category, default unit).

Ids are stable slugs shared with the land service (allowed_crops) and the seed data, so never
rename one; add new crops in a migration.
"""

CROPS: list[tuple[str, str, str, str]] = [
    # vegetables
    ("tomato", "Tomato", "vegetable", "kg"),
    ("onion", "Onion", "vegetable", "kg"),
    ("potato", "Potato", "vegetable", "kg"),
    ("brinjal", "Brinjal", "vegetable", "kg"),
    ("okra", "Okra (Bhindi)", "vegetable", "kg"),
    ("cauliflower", "Cauliflower", "vegetable", "piece"),
    ("cabbage", "Cabbage", "vegetable", "piece"),
    ("carrot", "Carrot", "vegetable", "kg"),
    ("green-chilli", "Green Chilli", "vegetable", "kg"),
    ("bottle-gourd", "Bottle Gourd (Lauki)", "vegetable", "piece"),
    ("bitter-gourd", "Bitter Gourd (Karela)", "vegetable", "kg"),
    ("cucumber", "Cucumber", "vegetable", "kg"),
    ("pumpkin", "Pumpkin", "vegetable", "kg"),
    ("peas", "Green Peas", "vegetable", "kg"),
    ("spinach", "Spinach (Palak)", "leafy", "bunch"),
    ("coriander", "Coriander", "leafy", "bunch"),
    ("methi", "Fenugreek (Methi)", "leafy", "bunch"),
    # fruits
    ("mango", "Mango", "fruit", "kg"),
    ("banana", "Banana", "fruit", "dozen"),
    ("guava", "Guava", "fruit", "kg"),
    ("papaya", "Papaya", "fruit", "kg"),
    ("watermelon", "Watermelon", "fruit", "piece"),
    # grains and pulses
    ("rice", "Rice (Paddy)", "grain", "quintal"),
    ("wheat", "Wheat", "grain", "quintal"),
    ("maize", "Maize", "grain", "quintal"),
    ("millet", "Millet (Bajra)", "grain", "quintal"),
    ("arhar", "Pigeon Pea (Arhar)", "pulse", "quintal"),
    ("chana", "Chickpea (Chana)", "pulse", "quintal"),
    ("moong", "Green Gram (Moong)", "pulse", "quintal"),
    # oilseeds and others
    ("soybean", "Soybean", "oilseed", "quintal"),
    ("groundnut", "Groundnut", "oilseed", "quintal"),
    ("mustard", "Mustard", "oilseed", "quintal"),
    ("sugarcane", "Sugarcane", "cash", "quintal"),
    ("garlic", "Garlic", "spice", "kg"),
    ("ginger", "Ginger", "spice", "kg"),
    ("turmeric", "Turmeric", "spice", "kg"),
]
