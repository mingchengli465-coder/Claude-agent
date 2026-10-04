"""Industry demos: a pretend shop for each kind of business we write to, with its own AI assistant.

A guest-house owner who opens /demo/bnb chats with "Willow Cottage B&B"; a florist gets
"Petal & Stem". Each answers from its own sample price list, exactly as a real one would, so the
owner sees what it would be like on their own site. The cold emails link to the matching demo.
"""

from __future__ import annotations

DEMO_PERSONA = (
    "你是「{name}」的 AI 客服助理。这是一个示范页面：其他店主在这里试用，看看 AI 客服放进自己店里是什么样子。"
    "你就当自己真的是这家店的助理来接待客人，资料里的店名、价格、规定都是示范用的。客人没问起的话，不用说这是示范。"
)

DEMOS: dict[str, dict] = {
    "bnb": {
        "name": "Willow Cottage B&B",
        "label": "Bed & Breakfast", "label_zh": "民宿",
        "where": "Keswick, Lake District",
        "questions": ["Is a double room free next Friday and Saturday?", "Can we bring our small dog?",
                      "We'll arrive around 9:30pm, is that OK?"],
        "catalog": """shop: Willow Cottage B&B (a demo shop)
location: Keswick, in the Lake District, UK. 5 minutes' walk from the town centre.
rooms (all prices in US$, breakfast included):
  - Double room, en-suite: US$120 a night for two
  - Twin room, en-suite: US$110 a night
  - Family room, sleeps 4: US$165 a night
availability: the owner checks the calendar and confirms every booking; never promise a room is free.
check-in: 3pm to 8pm. Later arrivals are fine if guests tell us their time in advance; a key box is available.
check-out: 10:30am
breakfast: full English or vegetarian, 7:30 to 9am. Gluten-free and vegan on request.
parking: free, 4 spaces on site
dogs: one small, well-behaved dog is welcome in the twin room, US$15 a night
minimum stay: 2 nights on Fridays and Saturdays
cancellation: free up to 7 days before arrival
how to book: one night's deposit secures the room; the rest is paid on arrival by card or bank transfer
extras: packed lunches US$12; drying room for walking boots""",
    },
    "florist": {
        "name": "Petal & Stem",
        "label": "Florist", "label_zh": "花店",
        "where": "Brighton",
        "questions": ["Can you deliver a birthday bouquet tomorrow morning?", "How much is a bridal bouquet?",
                      "Do you have anything around US$50?"],
        "catalog": """shop: Petal & Stem florist (a demo shop)
location: Brighton, UK. Shop open Monday to Saturday, 9am to 5:30pm.
bouquets (prices in US$): seasonal hand-tied bouquet from US$45, US$65 and US$90 sizes; roses (12) US$75; plants from US$25
same-day delivery: order by 1pm, Monday to Saturday, within 8 miles of Brighton; delivery US$8
next-day delivery: order any time before 6pm
card message: free, written by hand
weddings: bridal bouquet from US$140, buttonholes US$12 each, table centrepieces from US$45. Wedding orders need a consultation; the owner arranges it.
funerals: tributes from US$70, at least 2 days' notice
payment: online by card when the order is confirmed
the owner confirms every order and delivery slot""",
    },
    "bakery": {
        "name": "Butter & Bloom Cakes",
        "label": "Cake shop", "label_zh": "蛋糕店",
        "where": "Leeds",
        "questions": ["How much is a 2-tier birthday cake for 30 people?", "Do you do gluten-free cupcakes?",
                      "Can I order a cake for this Saturday?"],
        "catalog": """shop: Butter & Bloom Cakes (a demo shop)
location: Leeds, UK. Collection from the shop, Tuesday to Saturday, 10am to 5pm.
celebration cakes (prices in US$): 6-inch (8–10 people) from US$45; 8-inch (14–18 people) from US$65;
  2-tier (about 30 people) from US$150. Decorations, names and themes are priced by the owner.
flavours: vanilla & raspberry, chocolate fudge, lemon drizzle, red velvet, salted caramel
cupcakes: box of 12 US$30; gluten-free cupcakes available, box of 12 US$34; vegan on request
notice: at least 5 days for celebration cakes, 2 days for cupcakes. Rush orders only if the owner says yes.
delivery: within 10 miles of Leeds, US$12
deposit: 50% to confirm an order; the owner confirms dates and designs
allergens: made in a kitchen that uses nuts; cannot guarantee nut-free""",
    },
    "beauty": {
        "name": "Glow Studio",
        "label": "Beauty studio", "label_zh": "美容院",
        "where": "Manchester",
        "questions": ["Do you have anything free on Friday after 5pm?", "How much is a gel manicure?",
                      "What's good for dry skin?"],
        "catalog": """shop: Glow Studio beauty salon (a demo shop)
location: Manchester, UK. Open Tuesday to Saturday, 10am to 7pm (Thursday until 8pm).
treatments (prices in US$):
  - Signature facial, 60 minutes: US$70
  - Hydrating facial for dry skin, 45 minutes: US$60
  - Gel manicure, 45 minutes: US$35; gel removal US$10
  - Brow shape and tint, 30 minutes: US$28
  - Lash lift, 50 minutes: US$55 (needs a patch test 48 hours before)
booking: the owner confirms every appointment time; never promise a slot is free.
cancellation: please give 24 hours' notice
payment: card or cash after the treatment
gift vouchers: any amount, sent by email""",
    },
    "groomer": {
        "name": "Happy Paws Grooming",
        "label": "Dog grooming", "label_zh": "寵物美容",
        "where": "Bristol",
        "questions": ["How much is a full groom for a cockapoo?", "Can my 14-week puppy come in?",
                      "Do you have space next week?"],
        "catalog": """shop: Happy Paws Grooming (a demo shop)
location: Bristol, UK. Open Monday to Friday, 8:30am to 4pm, Saturday 9am to 1pm.
services (prices in US$, depend on size and coat):
  - Full groom (bath, dry, cut, nails, ears): small dogs from US$50, medium from US$60, large from US$75
  - Cockapoos and doodles: from US$65
  - Bath and brush: from US$35
  - Nail trim: US$12, no appointment needed on weekdays
puppies: first gentle puppy visit from 12 weeks, after their vaccinations, US$30
nervous dogs: quiet appointments first thing in the morning
booking: the owner confirms every appointment; never promise a time is free
how long: most grooms take 2 to 3 hours; we text when the dog is ready""",
    },
    "tutor": {
        "name": "Bright Minds Tuition",
        "label": "Tuition centre", "label_zh": "補習中心",
        "where": "London",
        "questions": ["My daughter is in Year 5, can you help with the 11+?", "How much are GCSE maths lessons?",
                      "Is there a free trial lesson?"],
        "catalog": """shop: Bright Minds Tuition centre (a demo)
location: North London, UK. Lessons after school on weekdays and on Saturday mornings.
courses (prices in US$):
  - 11+ preparation for Year 4 and Year 5: small groups of up to 6, US$35 a lesson (90 minutes)
  - GCSE maths and English: small groups, US$30 a lesson (60 minutes); one-to-one US$55
  - Primary maths and English, Years 2 to 6: US$25 a lesson
first step: a free 30-minute assessment, then a recommendation of the right group
trial lesson: the first group lesson is free after the assessment
timetable: the owner confirms which groups have places; never promise a place is free
payment: monthly, by bank transfer""",
    },
}

DEFAULT_KIND = "bnb"
