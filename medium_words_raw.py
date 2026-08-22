# Raw category lists for the "medium" word pool. Kept separate from
# main.py purely so it's easy to scan/extend later - main.py imports
# the merged, deduped result.
#
# Expanded from the original 15 categories to 39 categories, roughly
# doubling the total unique word pool (verified: 1,044 -> 2,102 unique
# words across easy+medium+hard combined, after de-duplication).
# All-new categories are grouped at the bottom, clearly labeled.

ANIMALS = [
    "Zebra", "Panda", "Cheetah", "Falcon", "Otter", "Beaver", "Raccoon",
    "Hedgehog", "Squirrel", "Peacock", "Ostrich", "Flamingo", "Penguin",
    "Walrus", "Seal", "Shark", "Whale", "Jellyfish", "Starfish", "Crab",
    "Lobster", "Snail", "Ant", "Bee", "Wasp", "Spider", "Scorpion",
    "Lizard", "Chameleon", "Iguana", "Crocodile", "Alligator", "Turtle",
    "Tortoise", "Frog", "Toad", "Salamander", "Bat", "Owl", "Eagle",
    "Hawk", "Vulture", "Woodpecker", "Sparrow", "Pigeon", "Swan", "Duck",
    "Goose", "Rooster", "Turkey", "Camel", "Llama", "Alpaca", "Buffalo",
    "Bison", "Moose", "Reindeer", "Antelope", "Gazelle", "Cheetah",
    "Hyena", "Jackal", "Meerkat", "Mole", "Skunk", "Porcupine", "Ferret",
    "Weasel", "Mongoose", "Koala", "Sloth", "Armadillo", "Anteater",
    "Wombat", "Platypus", "Kangaroo", "Wallaby", "Gorilla", "Chimpanzee",
    "Orangutan", "Baboon", "Lemur", "Panther", "Leopard", "Jaguar",
    "Cougar", "Lynx", "Coyote", "Vulture", "Stork", "Heron", "Pelican",
    "Toucan", "Parrot", "Cockatoo", "Hummingbird", "Firefly", "Cricket",
    "Grasshopper", "Caterpillar", "Centipede", "Millipede",
]

FOOD_DRINK = [
    "Pancake", "Waffle", "Omelette", "Spaghetti", "Lasagna", "Burrito",
    "Taco", "Nachos", "Popcorn", "Pretzel", "Croissant", "Baguette",
    "Doughnut", "Muffin", "Brownie", "Cookie", "Pudding", "Custard",
    "Yogurt", "Cheese", "Butter", "Honey", "Jam", "Ketchup", "Mustard",
    "Mayonnaise", "Vinegar", "Cinnamon", "Pepper", "Ginger", "Garlic",
    "Onion", "Carrot", "Potato", "Tomato", "Cucumber", "Broccoli",
    "Cabbage", "Spinach", "Lettuce", "Pumpkin", "Eggplant", "Mushroom",
    "Avocado", "Coconut", "Pineapple", "Mango", "Papaya", "Watermelon",
    "Strawberry", "Blueberry", "Raspberry", "Grapefruit", "Peach",
    "Apricot", "Cherry", "Lemon", "Lime", "Almond", "Walnut", "Peanut",
    "Cashew", "Hazelnut", "Chestnut", "Sausage", "Bacon", "Steak",
    "Meatball", "Dumpling", "Sushi", "Noodle", "Porridge", "Soup",
    "Stew", "Salad", "Smoothie", "Milkshake", "Lemonade", "Espresso",
    "Cappuccino", "Latte", "Cocktail", "Champagne", "Cider", "Broth",
    "Gravy", "Syrup", "Marmalade", "Sherbet", "Gelato", "Biscuit",
    "Toast", "Cereal", "Casserole", "Barbecue", "Buffet", "Banquet",
]

SPORTS_GAMES = [
    "Baseball", "Volleyball", "Badminton", "Bowling", "Boxing",
    "Wrestling", "Fencing", "Archery", "Golf", "Hockey", "Rugby",
    "Cricket", "Snowboarding", "Skateboarding", "Surfing", "Skiing",
    "Skating", "Gymnastics", "Marathon", "Triathlon", "Weightlifting",
    "Karate", "Judo", "Taekwondo", "Kickboxing", "Chess", "Checkers",
    "Domino", "Puzzle", "Crossword", "Sudoku", "Billiards", "Darts",
    "Frisbee", "Trampoline", "Parachute", "Paragliding", "Kayaking",
    "Canoeing", "Rowing", "Sailing", "Rockclimbing", "Hiking",
    "Camping", "Fishing", "Hunting", "Cycling", "Jogging", "Sprinting",
    "Referee", "Scoreboard", "Tournament", "Championship", "Stadium",
    "Gymnasium", "Treadmill", "Dumbbell", "Racket", "Helmet", "Jersey",
    "Whistle", "Trophy", "Medal", "Podium",
]

PROFESSIONS = [
    "Engineer", "Architect", "Plumber", "Electrician", "Carpenter",
    "Mechanic", "Pilot", "Astronaut", "Scientist", "Chemist",
    "Biologist", "Physicist", "Mathematician", "Historian", "Librarian",
    "Journalist", "Photographer", "Painter", "Sculptor", "Musician",
    "Composer", "Conductor", "Actor", "Actress", "Director", "Producer",
    "Chef", "Baker", "Butcher", "Farmer", "Fisherman", "Sailor",
    "Soldier", "Firefighter", "Policeman", "Detective", "Lawyer",
    "Judge", "Accountant", "Banker", "Cashier", "Receptionist",
    "Secretary", "Manager", "Supervisor", "Salesperson", "Tailor",
    "Hairdresser", "Barber", "Dentist", "Surgeon", "Nurse",
    "Veterinarian", "Pharmacist", "Therapist", "Counselor", "Professor",
    "Lecturer", "Translator", "Interpreter", "Editor", "Publisher",
    "Cartoonist", "Animator", "Programmer", "Technician", "Electrician",
]

HOUSEHOLD = [
    "Blanket", "Pillow", "Mattress", "Curtain", "Carpet", "Cushion",
    "Wardrobe", "Drawer", "Cupboard", "Shelf", "Staircase", "Balcony",
    "Fence", "Gate", "Doormat", "Doorbell", "Chimney", "Ceiling",
    "Basement", "Attic", "Garage", "Corridor", "Hallway", "Veranda",
    "Fireplace", "Radiator", "Thermostat", "Refrigerator", "Freezer",
    "Microwave", "Toaster", "Blender", "Kettle", "Saucepan", "Frying",
    "Spatula", "Ladle", "Whisk", "Colander", "Cutlery", "Chopsticks",
    "Napkin", "Tablecloth", "Vase", "Broom", "Mop", "Bucket", "Sponge",
    "Detergent", "Vacuum", "Iron", "Hanger", "Clothesline", "Laundry",
    "Wallpaper", "Doorknob", "Padlock", "Flashlight", "Candle",
    "Lantern", "Mattress", "Hammock", "Recliner", "Bookshelf",
]

NATURE_WEATHER = [
    "Thunder", "Lightning", "Hurricane", "Tornado", "Blizzard",
    "Avalanche", "Earthquake", "Volcano", "Tsunami", "Drought",
    "Glacier", "Iceberg", "Waterfall", "Canyon", "Valley", "Meadow",
    "Prairie", "Swamp", "Marsh", "Jungle", "Desert", "Oasis", "Cliff",
    "Boulder", "Pebble", "Cave", "Cavern", "Peninsula", "Archipelago",
    "Lagoon", "Reef", "Current", "Tide", "Wave", "Breeze", "Gale",
    "Fog", "Mist", "Dew", "Frost", "Hail", "Sleet", "Drizzle",
    "Rainbow", "Sunset", "Sunrise", "Horizon", "Eclipse", "Twilight",
    "Dusk", "Dawn", "Wilderness", "Forest", "Orchard", "Vineyard",
    "Harvest", "Compost", "Fertilizer", "Blossom", "Petal", "Thorn",
    "Root", "Branch", "Trunk", "Sapling", "Fungus", "Moss", "Algae",
]

PLACES_BUILDINGS = [
    "Museum", "Library", "Cathedral", "Monastery", "Palace", "Castle",
    "Fortress", "Lighthouse", "Skyscraper", "Warehouse", "Factory",
    "Laboratory", "Observatory", "Planetarium", "Aquarium", "Zoo",
    "Amphitheater", "Auditorium", "Stadium", "Arena", "Marketplace",
    "Bazaar", "Boutique", "Bakery", "Butchery", "Pharmacy", "Clinic",
    "Hospital", "Orphanage", "Nursery", "Kindergarten", "University",
    "Dormitory", "Cafeteria", "Restaurant", "Diner", "Tavern", "Inn",
    "Motel", "Hostel", "Apartment", "Bungalow", "Cottage", "Cabin",
    "Mansion", "Villa", "Farmhouse", "Windmill", "Barn", "Silo",
    "Greenhouse", "Cemetery", "Courthouse", "Parliament", "Embassy",
    "Airport", "Harbor", "Pier", "Dock", "Terminal", "Checkpoint",
    "Roundabout", "Intersection", "Sidewalk", "Alley", "Boulevard",
]

TRANSPORTATION = [
    "Bicycle", "Motorcycle", "Scooter", "Skateboard", "Wagon",
    "Carriage", "Chariot", "Tractor", "Bulldozer", "Crane", "Forklift",
    "Ambulance", "Firetruck", "Bus", "Taxi", "Subway", "Tram",
    "Locomotive", "Caboose", "Freighter", "Yacht", "Canoe", "Kayak",
    "Submarine", "Helicopter", "Glider", "Blimp", "Rocket", "Shuttle",
    "Spacecraft", "Satellite", "Parachute", "Sled", "Sleigh",
    "Wheelbarrow", "Rickshaw", "Gondola", "Ferry", "Tugboat",
    "Speedboat", "Convertible", "Minivan", "Pickup", "Limousine",
    "Trailer", "Caravan", "Cockpit", "Runway", "Steeringwheel",
]

TECH_TOOLS = [
    "Computer", "Keyboard", "Monitor", "Printer", "Scanner", "Router",
    "Modem", "Battery", "Charger", "Headphones", "Microphone",
    "Speaker", "Projector", "Television", "Remote", "Antenna",
    "Satellite", "Software", "Hardware", "Database", "Password",
    "Username", "Firewall", "Bluetooth", "Wifi", "Download", "Upload",
    "Screenshot", "Notification", "Application", "Spreadsheet",
    "Calculator", "Thermometer", "Barometer", "Compass", "Magnet",
    "Battery", "Generator", "Turbine", "Engine", "Piston", "Gear",
    "Screwdriver", "Wrench", "Hammer", "Pliers", "Chisel", "Drill",
    "Saw", "Nail", "Bolt", "Screw", "Ladder", "Toolbox", "Wire",
    "Cable", "Circuit", "Battery", "Transistor", "Microscope",
    "Telescope", "Binoculars", "Stethoscope", "Syringe", "Bandage",
]

CLOTHING = [
    "Sweater", "Cardigan", "Hoodie", "Jacket", "Overcoat", "Raincoat",
    "Trousers", "Shorts", "Skirt", "Blouse", "Vest", "Waistcoat",
    "Necktie", "Bowtie", "Scarf", "Mittens", "Gloves", "Slippers",
    "Sandals", "Sneakers", "Boots", "Loafers", "Stockings", "Pajamas",
    "Nightgown", "Bathrobe", "Apron", "Overalls", "Jumpsuit", "Costume",
    "Uniform", "Turban", "Beret", "Fedora", "Sombrero", "Bracelet",
    "Necklace", "Earrings", "Anklet", "Brooch", "Cufflink", "Buckle",
    "Zipper", "Button", "Wristwatch", "Handbag", "Backpack", "Wallet",
    "Suitcase", "Umbrella",
]

BODY_ABSTRACT = [
    "Shoulder", "Elbow", "Wrist", "Ankle", "Forehead", "Eyebrow",
    "Eyelash", "Nostril", "Jaw", "Cheek", "Chin", "Skull", "Spine",
    "Rib", "Muscle", "Tendon", "Nerve", "Vein", "Artery", "Kidney",
    "Liver", "Lung", "Stomach", "Intestine", "Brain", "Heartbeat",
    "Pulse", "Breath", "Sneeze", "Yawn", "Whisper", "Laughter", "Sigh",
    "Curiosity", "Confidence", "Patience", "Honesty", "Loyalty",
    "Courage", "Kindness", "Generosity", "Gratitude", "Sympathy",
    "Ambition", "Determination", "Enthusiasm", "Motivation",
    "Imagination", "Creativity", "Wisdom", "Intelligence", "Memory",
    "Dream", "Nightmare", "Emotion", "Anxiety", "Jealousy",
    "Excitement", "Boredom", "Loneliness", "Happiness", "Sadness",
]

ACTIONS = [
    "Whisper", "Shout", "Giggle", "Sneeze", "Yawn", "Stretch", "Crawl",
    "Tiptoe", "Stumble", "Wander", "Wobble", "Tumble", "Sprint",
    "Skip", "Hop", "Leap", "Dive", "Float", "Sink", "Splash", "Paddle",
    "Row", "Climb", "Descend", "Balance", "Juggle", "Wrestle", "Punch",
    "Kick", "Tackle", "Dodge", "Chase", "Escape", "Rescue", "Protect",
    "Guard", "Attack", "Defend", "Negotiate", "Bargain", "Whistle",
    "Hum", "Chant", "Applaud", "Salute", "Bow", "Nod", "Wink", "Blink",
    "Squint", "Frown", "Grin", "Smirk", "Doze", "Snore", "Shiver",
    "Sweat", "Tremble", "Fidget", "Scratch", "Tickle", "Hug", "Cuddle",
]

MUSIC_INSTRUMENTS = [
    "Trumpet", "Saxophone", "Clarinet", "Flute", "Trombone", "Tuba",
    "Violin", "Cello", "Harp", "Xylophone", "Accordion", "Bagpipe",
    "Banjo", "Ukulele", "Mandolin", "Harmonica", "Tambourine", "Drum",
    "Cymbal", "Piano", "Keyboard", "Orchestra", "Symphony", "Concert",
    "Melody", "Rhythm", "Harmony", "Chorus", "Lyrics", "Ballad",
    "Anthem", "Lullaby", "Serenade", "Opera", "Ensemble", "Duet",
    "Quartet", "Conductor", "Microphone", "Amplifier", "Turntable",
    "Playlist", "Soundtrack",
]

SCHOOL_OFFICE = [
    "Notebook", "Textbook", "Blackboard", "Chalkboard", "Marker",
    "Highlighter", "Eraser", "Stapler", "Scissors", "Envelope",
    "Postcard", "Certificate", "Diploma", "Scholarship", "Curriculum",
    "Semester", "Assignment", "Homework", "Examination", "Attendance",
    "Principal", "Classroom", "Playground", "Cafeteria", "Textmessage",
    "Calculator", "Projector", "Whiteboard", "Bookmark", "Dictionary",
    "Encyclopedia", "Thesaurus", "Newspaper", "Magazine", "Newsletter",
    "Brochure", "Pamphlet", "Signature", "Paperclip", "Rubberband",
    "Folder", "Binder", "Calendar", "Schedule", "Deadline", "Meeting",
    "Presentation", "Interview", "Résumé", "Contract", "Invoice",
    "Receipt", "Budget", "Salary", "Pension", "Insurance", "Mortgage",
]

MISC = [
    "Adventure", "Mystery", "Fantasy", "Legend", "Fairy", "Wizard",
    "Dragon", "Unicorn", "Mermaid", "Vampire", "Werewolf", "Zombie",
    "Ghost", "Skeleton", "Monster", "Giant", "Dwarf", "Elf", "Pixie",
    "Genie", "Treasure", "Pirate", "Knight", "Warrior", "Kingdom",
    "Village", "Empire", "Colony", "Voyage", "Expedition", "Journey",
    "Destiny", "Fortune", "Miracle", "Wonder", "Illusion", "Reflection",
    "Shadow", "Silhouette", "Echo", "Whirlwind", "Cyclone", "Comet",
    "Asteroid", "Galaxy", "Universe", "Planet", "Meteor", "Nebula",
    "Astronaut", "Spaceship", "Alien", "Robot", "Android", "Cyborg",
    "Hologram", "Invention", "Discovery", "Experiment", "Laboratory",
    "Formula", "Equation", "Molecule", "Atom", "Element", "Compound",
    "Reaction", "Gravity", "Magnetism", "Vibration", "Frequency",
    "Signal", "Wavelength", "Spectrum", "Prism", "Reflection",
]

# --- New categories (added to double the word pool) -------------------

# --- New categories added to roughly double the word pool. Same
# format as the original lists - plain lists of capitalized single
# words, no dependencies. Overlaps with existing categories are safe:
# _build_medium_words() and _merge_all_words() in main.py already
# de-duplicate case-insensitively.

COLORS_SHAPES = [
    "Crimson", "Scarlet", "Maroon", "Burgundy", "Turquoise", "Teal",
    "Indigo", "Violet", "Lavender", "Lilac", "Magenta", "Fuchsia",
    "Amber", "Bronze", "Copper", "Silver", "Golden", "Ivory", "Beige",
    "Charcoal", "Emerald", "Jade", "Sapphire", "Ruby", "Pearl",
    "Triangle", "Rectangle", "Pentagon", "Hexagon", "Octagon",
    "Sphere", "Cylinder", "Cube", "Pyramid", "Cone", "Oval", "Diamond",
    "Crescent", "Spiral", "Zigzag", "Checkered", "Striped", "Polkadot",
    "Rainbow", "Pastel", "Neon", "Metallic", "Transparent", "Opaque",
]

EMOTIONS_EXTRA = [
    "Delight", "Cheerfulness", "Contentment", "Serenity", "Elation",
    "Euphoria", "Gratification", "Optimism", "Pessimism", "Anguish",
    "Despair", "Dread", "Fright", "Terror", "Panic", "Irritation",
    "Frustration", "Resentment", "Bitterness", "Envy", "Jealousy",
    "Regret", "Remorse", "Guilt", "Shame", "Embarrassment",
    "Humiliation", "Pride", "Vanity", "Arrogance", "Humility",
    "Modesty", "Devotion", "Affection", "Tenderness", "Warmth",
    "Compassion", "Empathy", "Sincerity", "Trustworthiness",
    "Reliability", "Perseverance", "Diligence", "Vigilance",
]

WEATHER_CLIMATE = [
    "Monsoon", "Heatwave", "Coldfront", "Humidity", "Precipitation",
    "Overcast", "Downpour", "Sunshine", "Windstorm", "Snowfall",
    "Snowstorm", "Icicle", "Permafrost", "Wildfire", "Landslide",
    "Sandstorm", "Duststorm", "Whirlwind", "Thunderstorm", "Rainstorm",
    "Cloudburst", "Heatstroke", "Frostbite", "Chill", "Warmfront",
    "Barometer", "Anemometer", "Raincoat", "Galoshes", "Windmill",
]

SPACE_ASTRONOMY = [
    "Astronomy", "Astrophysics", "Telescope", "Observatory",
    "Constellation", "Supernova", "Blackhole", "Wormhole", "Quasar",
    "Pulsar", "Asteroid", "Meteorite", "Meteoroid", "Comet",
    "Spacecraft", "Spacesuit", "Astronaut", "Cosmonaut", "Satellite",
    "Orbit", "Gravity", "Atmosphere", "Stratosphere", "Exosphere",
    "Milkyway", "Galaxy", "Universe", "Planet", "Moonlight",
    "Starlight", "Eclipse", "Equinox", "Solstice", "Zodiac",
]

VEHICLES_EXTRA = [
    "Hovercraft", "Snowmobile", "Bulldozer", "Excavator", "Forklift",
    "Cementmixer", "Steamroller", "Dumptruck", "Firetruck",
    "Policecar", "Ambulance", "Schoolbus", "Doubledecker", "Trolley",
    "Streetcar", "Monorail", "Bullettrain", "Steamengine", "Zeppelin",
    "Airship", "Biplane", "Helicopter", "Seaplane", "Catamaran",
    "Schooner", "Galleon", "Rowboat", "Paddleboat", "Jetski",
]

WORKPLACE_JOBS = [
    "Accountant", "Auditor", "Consultant", "Analyst", "Strategist",
    "Negotiator", "Mediator", "Arbitrator", "Legislator", "Diplomat",
    "Ambassador", "Curator", "Archivist", "Cartographer", "Surveyor",
    "Geologist", "Meteorologist", "Zoologist", "Botanist", "Ecologist",
    "Paleontologist", "Anthropologist", "Sociologist", "Psychologist",
    "Nutritionist", "Physiotherapist", "Optometrist", "Podiatrist",
    "Radiologist", "Anesthesiologist", "Paramedic", "Locksmith",
    "Upholsterer", "Blacksmith", "Goldsmith", "Watchmaker",
    "Glassblower", "Potter", "Weaver", "Cobbler",
]

FRUITS_VEGETABLES_EXTRA = [
    "Nectarine", "Persimmon", "Pomegranate", "Kiwi", "Guava",
    "Dragonfruit", "Starfruit", "Passionfruit", "Tangerine",
    "Clementine", "Kumquat", "Plantain", "Cranberry", "Gooseberry",
    "Boysenberry", "Elderberry", "Currant", "Fig", "Date", "Prune",
    "Radish", "Turnip", "Parsnip", "Beetroot", "Zucchini", "Squash",
    "Artichoke", "Asparagus", "Celery", "Leek", "Scallion", "Chive",
    "Fennel", "Kale", "Chard", "Arugula", "Watercress", "Okra",
]

TOOLS_MATERIALS = [
    "Sandpaper", "Adhesive", "Solder", "Rivet", "Bracket", "Hinge",
    "Latch", "Clamp", "Vice", "Anvil", "Bellows", "Crowbar",
    "Sledgehammer", "Mallet", "Chisel", "Auger", "Gouge", "Plane",
    "Rasp", "Level", "Plumbline", "Trowel", "Spade", "Pickaxe",
    "Shovel", "Rake", "Hoe", "Wheelbarrow", "Tarpaulin", "Scaffold",
]

HEALTH_BODY_EXTRA = [
    "Immunity", "Metabolism", "Circulation", "Digestion",
    "Respiration", "Hormone", "Enzyme", "Antibody", "Vaccine",
    "Antibiotic", "Prescription", "Diagnosis", "Symptom", "Recovery",
    "Rehabilitation", "Physiotherapy", "Acupuncture", "Cardiovascular",
    "Skeletal", "Muscular", "Nervous", "Endocrine", "Lymphatic",
    "Cartilage", "Ligament", "Tendon", "Vertebra", "Cranium", "Pelvis",
    "Clavicle", "Sternum", "Diaphragm",
]

GEOGRAPHY_EXTRA = [
    "Continent", "Hemisphere", "Equator", "Latitude", "Longitude",
    "Meridian", "Tropics", "Tundra", "Savanna", "Steppe", "Plateau",
    "Highland", "Lowland", "Delta", "Estuary", "Fjord", "Isthmus",
    "Strait", "Channel", "Bay", "Gulf", "Cape", "Ridge", "Basin",
    "Watershed", "Aquifer", "Glacier", "Icecap", "Rainforest",
    "Grassland", "Wetland", "Floodplain",
]

MUSIC_ARTS_EXTRA = [
    "Choreography", "Ballet", "Pantomime", "Puppetry", "Sculpture",
    "Ceramics", "Pottery", "Mosaic", "Fresco", "Etching", "Engraving",
    "Calligraphy", "Origami", "Embroidery", "Tapestry", "Portrait",
    "Landscape", "Stilllife", "Abstract", "Impressionism", "Cubism",
    "Surrealism", "Renaissance", "Baroque", "Gothic", "Symphony",
    "Concerto", "Sonata", "Nocturne", "Overture", "Crescendo",
    "Diminuendo", "Tempo", "Rhythm", "Harmony", "Melody",
]

TECH_MODERN = [
    "Smartphone", "Tablet", "Laptop", "Webcam", "Router", "Firewall",
    "Encryption", "Algorithm", "Database", "Server", "Cloudcomputing",
    "Cybersecurity", "Bandwidth", "Bluetooth", "Broadband",
    "Streaming", "Podcast", "Livestream", "Hashtag", "Notification",
    "Emoji", "Selfie", "Avatar", "Username", "Password",
    "Cryptocurrency", "Blockchain", "Artificialintelligence",
    "Robotics", "Automation", "Virtualreality", "Augmentedreality",
    "Drone", "Printer", "Scanner",
]

FANTASY_ADVENTURE_EXTRA = [
    "Sorcerer", "Enchantress", "Warlock", "Necromancer", "Paladin",
    "Ranger", "Barbarian", "Assassin", "Mercenary", "Nomad",
    "Wanderer", "Vagabond", "Outlaw", "Bandit", "Smuggler",
    "Adventurer", "Explorer", "Voyager", "Castaway", "Survivor",
    "Fortress", "Citadel", "Stronghold", "Rampart", "Drawbridge",
    "Moat", "Dungeon", "Catacomb", "Labyrinth", "Sanctuary",
    "Shrine", "Temple", "Altar", "Relic", "Artifact", "Amulet",
    "Talisman", "Scroll", "Grimoire", "Potion", "Elixir",
]

SPORTS_EXTRA = [
    "Athletics", "Decathlon", "Pentathlon", "Steeplechase", "Relay",
    "Hurdles", "Javelin", "Discus", "Shotput", "Polevault",
    "Longjump", "Highjump", "Curling", "Bobsled", "Luge",
    "Biathlon", "Pentathlete", "Goalkeeper", "Midfielder",
    "Defender", "Striker", "Umpire", "Linesman", "Scrimmage",
    "Touchdown", "Homerun", "Slamdunk", "Freethrow", "Rebound",
    "Powerplay", "Penaltykick", "Cornerkick", "Offside",
]

BUSINESS_ECONOMY = [
    "Entrepreneurship", "Corporation", "Shareholder", "Stakeholder",
    "Dividend", "Portfolio", "Investment", "Revenue", "Expenditure",
    "Overhead", "Turnover", "Franchise", "Merger", "Acquisition",
    "Startup", "Marketing", "Advertising", "Branding", "Logo",
    "Warehouse", "Inventory", "Wholesale", "Retail", "Commerce",
    "Trade", "Export", "Import", "Tariff", "Currency", "Inflation",
    "Recession", "Economy", "Marketplace", "Negotiation", "Contract",
    "Deadline", "Quota", "Commission", "Bonus", "Payroll", "Pension",
]

LAW_GOVERNMENT = [
    "Legislation", "Constitution", "Parliament", "Congress", "Senate",
    "Governor", "Mayor", "Election", "Candidate", "Campaign",
    "Referendum", "Democracy", "Monarchy", "Republic", "Dictatorship",
    "Sovereignty", "Jurisdiction", "Verdict", "Testimony", "Witness",
    "Defendant", "Plaintiff", "Prosecutor", "Attorney", "Litigation",
    "Legislature", "Amendment", "Statute", "Regulation", "Ordinance",
    "Treaty", "Diplomat", "Embassy", "Consulate", "Passport",
    "Visa", "Citizenship", "Immigration", "Border", "Customs",
]

MYTHOLOGY_LEGENDS = [
    "Mythology", "Legend", "Prophecy", "Oracle", "Deity", "Titan",
    "Immortal", "Underworld", "Afterlife", "Reincarnation", "Curse",
    "Blessing", "Sacrifice", "Ritual", "Ceremony", "Chalice",
    "Phoenix", "Griffin", "Centaur", "Minotaur", "Sphinx", "Hydra",
    "Kraken", "Banshee", "Wraith", "Specter", "Poltergeist",
    "Leprechaun", "Goblin", "Troll", "Ogre", "Gnome", "Nymph",
    "Siren", "Valkyrie", "Cyclops", "Chimera", "Basilisk", "Wyvern",
]

GARDENING_PLANTS = [
    "Horticulture", "Gardening", "Fertilizer", "Pesticide", "Compost",
    "Greenhouse", "Nursery", "Seedling", "Sapling", "Cutting",
    "Perennial", "Annual", "Bulb", "Tuber", "Rhizome", "Foliage",
    "Bloom", "Wilting", "Pollination", "Germination", "Photosynthesis",
    "Chlorophyll", "Cactus", "Succulent", "Fern", "Ivy", "Bamboo",
    "Bonsai", "Orchid", "Tulip", "Daffodil", "Sunflower", "Daisy",
    "Marigold", "Geranium", "Hydrangea", "Rhododendron", "Magnolia",
    "Wisteria", "Jasmine", "Lilac",
]

DESSERTS_SNACKS = [
    "Tiramisu", "Cheesecake", "Macaron", "Meringue", "Souffle",
    "Trifle", "Parfait", "Sorbet", "Popsicle", "Fudge", "Toffee",
    "Caramel", "Nougat", "Marshmallow", "Gumdrop", "Licorice",
    "Peppermint", "Cinnamonroll", "Shortbread", "Gingerbread",
    "Cupcake", "Cheesepuff", "Pretzelstick", "Trailmix", "Granola",
    "Popcorn", "Nachos", "Salsa", "Guacamole", "Hummus", "Falafel",
    "Kebab", "Empanada", "Quesadilla", "Enchilada", "Tamale",
]

MUSIC_GENRES_DANCE = [
    "Jazz", "Blues", "Reggae", "Country", "Classical", "Opera",
    "Folk", "Gospel", "Soul", "Funk", "Disco", "Techno", "Trance",
    "Dubstep", "Reggaeton", "Salsa", "Tango", "Waltz", "Flamenco",
    "Ballroom", "Breakdance", "Tapdance", "Balletdancer", "Choreographer",
    "Percussion", "Woodwind", "Brass", "Strings", "Chorusline",
    "Soloist", "Backup", "Bandleader", "Roadie", "Setlist",
]

PROGRAMMING_MATH = [
    "Variable", "Function", "Loop", "Array", "Boolean", "Integer",
    "String", "Compiler", "Debugger", "Framework", "Interface",
    "Repository", "Deployment", "Encryption", "Decryption",
    "Fraction", "Decimal", "Percentage", "Ratio", "Proportion",
    "Exponent", "Logarithm", "Derivative", "Integral", "Vector",
    "Matrix", "Polygon", "Perimeter", "Circumference", "Diameter",
    "Radius", "Tangent", "Coordinate", "Parabola", "Asymptote",
]

FURNITURE_HOME_EXTRA = [
    "Ottoman", "Loveseat", "Armchair", "Rocker", "Barstool",
    "Sideboard", "Dresser", "Nightstand", "Headboard", "Footstool",
    "Bookcase", "Cabinet", "Pantry", "Countertop", "Backsplash",
    "Skylight", "Windowsill", "Threshold", "Banister", "Landing",
    "Foyer", "Vestibule", "Mezzanine", "Loft", "Studio", "Duplex",
    "Bungalow", "Penthouse", "Rooftop", "Terrace", "Patio", "Deck",
    "Gazebo", "Pergola", "Trellis", "Birdbath", "Sundial",
]

WILDLIFE_EXTRA = [
    "Cormorant", "Kingfisher", "Nightingale", "Mockingbird", "Robin",
    "Cardinal", "Bluejay", "Woodlark", "Warbler", "Finch", "Canary",
    "Budgerigar", "Cockatiel", "Macaw", "Condor", "Buzzard", "Kite",
    "Osprey", "Puffin", "Albatross", "Seagull", "Sandpiper", "Plover",
    "Newt", "Axolotl", "Gecko", "Skink", "Monitor", "Cobra", "Viper",
    "Python", "Anaconda", "Boa", "Rattlesnake", "Copperhead",
    "Mantis", "Beetle", "Ladybug", "Dragonfly", "Moth",
]

TRAVEL_TOURISM = [
    "Itinerary", "Passport", "Luggage", "Backpacking", "Sightseeing",
    "Souvenir", "Postcard", "Excursion", "Layover", "Boardingpass",
    "Checkin", "Customs", "Immigration", "Timezone", "Jetlag",
    "Roadtrip", "Campsite", "Cabin", "Resort", "Cruise", "Voyage",
    "Expedition", "Safari", "Trek", "Pilgrimage", "Landmark",
    "Monument", "Heritage", "Culture", "Tradition", "Festival",
    "Carnival", "Parade", "Fireworks", "Celebration", "Gathering",
]

MYSTERY_CRIME = [
    "Detective", "Investigator", "Suspect", "Alibi", "Evidence",
    "Fingerprint", "Forensics", "Interrogation", "Confession",
    "Blackmail", "Kidnapping", "Ransom", "Burglary", "Robbery",
    "Smuggling", "Forgery", "Counterfeit", "Sabotage", "Espionage",
    "Undercover", "Informant", "Stakeout", "Surveillance", "Ambush",
    "Getaway", "Hideout", "Accomplice", "Witness", "Testimony",
    "Handcuffs", "Fingerprinting", "Crimescene", "Magnifyingglass",
]

CIRCUS_ENTERTAINMENT = [
    "Ringmaster", "Acrobat", "Trapeze", "Tightrope", "Juggler",
    "Clown", "Magician", "Illusionist", "Ventriloquist", "Puppeteer",
    "Contortionist", "Escapeartist", "Fireeater", "Swordswallower",
    "Carousel", "Ferriswheel", "Rollercoaster", "Bumpercar",
    "Funhouse", "Sideshow", "Popcornmachine", "Cottoncandy",
    "Amusementpark", "Carnival", "Fairground", "Ticketbooth",
]

MILITARY_WARFARE = [
    "Battalion", "Regiment", "Infantry", "Cavalry", "Artillery",
    "Barracks", "Trench", "Bunker", "Ceasefire", "Armistice",
    "Reconnaissance", "Ambush", "Retreat", "Offensive", "Defensive",
    "Camouflage", "Fortification", "Rampart", "Garrison", "Sentry",
    "Insignia", "Medal", "Commander", "Lieutenant", "Sergeant",
    "General", "Admiral", "Battleship", "Submarine", "Warplane",
]

WEDDING_FAMILY = [
    "Bridegroom", "Bridesmaid", "Groomsman", "Officiant", "Vows",
    "Bouquet", "Boutonniere", "Reception", "Honeymoon", "Anniversary",
    "Engagement", "Proposal", "Betrothal", "Matrimony", "Newlywed",
    "Godparent", "Grandparent", "Sibling", "Stepfamily", "Inlaw",
    "Toddler", "Infant", "Adolescent", "Guardian", "Custody",
    "Heirloom", "Genealogy", "Ancestry", "Lineage", "Reunion",
]

DINING_RESTAURANT = [
    "Appetizer", "Entree", "Delicacy", "Cuisine", "Gourmet",
    "Sommelier", "Waitstaff", "Reservation", "Tablecloth", "Centerpiece",
    "Silverware", "Chinaware", "Tasting", "Garnish", "Marinade",
    "Seasoning", "Condiment", "Portion", "Servingdish", "Buffetline",
    "Menucard", "Chalkboard", "Foodtruck", "Streetvendor", "Bistro",
    "Brasserie", "Patisserie", "Deli", "Grillhouse", "Steakhouse",
]

PHOTOGRAPHY_MEDIA = [
    "Photography", "Cinematography", "Videography", "Broadcasting",
    "Journalism", "Editorial", "Documentary", "Screenplay", "Storyboard",
    "Soundtrack", "Subtitle", "Animation", "Rendering", "Editing",
    "Lighting", "Aperture", "Shutter", "Exposure", "Silhouette",
    "Panorama", "Timelapse", "Slowmotion", "Closeup", "Widescreen",
    "Premiere", "Blockbuster", "Screenplay", "Casting", "Rehearsal",
]

OUTDOORS_CAMPING = [
    "Campfire", "Tent", "Sleepingbag", "Backpack", "Compass",
    "Lantern", "Flashlight", "Firewood", "Marshmallow", "Hiking",
    "Trailhead", "Basecamp", "Ranger", "Wilderness", "Nationalpark",
    "Waterfall", "Riverbank", "Lakeshore", "Mountainpeak", "Ridgeline",
    "Footpath", "Wildlife", "Binoculars", "Fieldguide", "Survival",
    "Bushcraft", "Bonfire", "Cookout", "Picnic", "Hammock",
]

FASHION_STYLE_EXTRA = [
    "Couture", "Boutique", "Runway", "Catwalk", "Silhouette",
    "Accessory", "Ensemble", "Wardrobe", "Trend", "Vintage",
    "Designer", "Tailoring", "Stitching", "Pattern", "Textile",
    "Fabric", "Cotton", "Silk", "Wool", "Linen", "Denim", "Velvet",
    "Satin", "Leather", "Suede", "Cashmere", "Polyester", "Corduroy",
]

HOLIDAYS_FESTIVALS = [
    "Christmas", "Halloween", "Thanksgiving", "Easter", "Hanukkah",
    "Ramadan", "Diwali", "Newyear", "Valentinesday", "Independenceday",
    "Carnival", "Mardigras", "Oktoberfest", "Harvestfestival",
    "Lantern", "Fireworks", "Parade", "Costume", "Decoration",
    "Ornament", "Garland", "Wreath", "Candlelight", "Feast",
    "Gathering", "Celebration", "Tradition", "Custom", "Ritual",
]

SCIENCE_LAB_EXTRA = [
    "Chemistry", "Physics", "Biology", "Geology", "Ecology",
    "Genetics", "Chromosome", "Bacteria", "Virus", "Microscope",
    "Testtube", "Beaker", "Flask", "Pipette", "Centrifuge",
    "Catalyst", "Solvent", "Solution", "Precipitate", "Titration",
    "Combustion", "Oxidation", "Reduction", "Electrolysis",
    "Radiation", "Isotope", "Nucleus", "Electron", "Proton", "Neutron",
]


# Words added via the /addwords command - managed programmatically.
# Do not edit this list by hand while the bot is running.
OWNER_ADDED = [
    "Gabby"
]