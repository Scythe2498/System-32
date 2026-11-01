r"""
generate_training_data.py — System 32 tool use training data generator.
Produces instruction/response pairs in Alpaca format for Unsloth fine-tuning.
Run from I:\Sys 32's Home\ to output training_data.jsonl
"""

import json
import random
import os

# ── Workflow names (pulled from USB JsonFiles) ─────────────────────────────────
WORKFLOWS = [
    "Sys 32 Body.json",
    "Sys 32 Face.json",
    "Upscale.json",
    "Remove Background.json",
    "Img2Img.json",
    "Video.json",
]

CHECKPOINTS = [
    "sd_xl_base_1.0.safetensors",
    "my_checkpoint.safetensors",
]

SUBJECTS = [
    "a fox on a hill", "a cyberpunk city at night", "a dragon in a forest",
    "a cute anime girl with blue hair", "a samurai standing in rain",
    "a futuristic spaceship interior", "a magical forest with glowing mushrooms",
    "a wolf howling at the moon", "a steampunk airship", "an underwater city",
    "a fox girl with fluffy ears", "a dark fantasy knight", "a cozy cabin in snow",
    "a neon-lit alley", "a field of lavender at sunset",
]

STYLES = [
    "photorealistic", "anime style", "oil painting", "watercolor",
    "digital art", "pencil sketch", "cinematic lighting", "HDR",
]

LORA_SUBJECTS = ["my fox girl character", "a specific art style", "my OC"]
DATASETS = ["foxgirl_dataset", "my_character", "style_dataset"]

FOLDERS = [
    "output", "models", "loras", "home", "reference",
    "I:\\System 32's Output\\generated images",
    "I:\\Sys 32's comfyui resources\\models",
]

MEMORY_TOPICS = [
    "my favorite checkpoint", "the fox lora settings",
    "my preferences", "last generation settings",
    "my voice preference",
]

PREFERENCES = [
    ("width", "1024"), ("height", "1024"), ("steps", "30"),
    ("cfg", "7"), ("checkpoint", "sd_xl_base_1.0.safetensors"),
    ("video_fps", "24"), ("video_duration", "5"),
]

# ── Tool call format ───────────────────────────────────────────────────────────

def tc(tool_name, **kwargs):
    """Format a tool call as the model would output it."""
    filtered = {k: v for k, v in kwargs.items() if v is not None}
    return json.dumps({"tool": tool_name, "params": filtered}, ensure_ascii=False)


# ── Training pair builders ─────────────────────────────────────────────────────

def image_generation_pairs():
    pairs = []
    for _ in range(40):
        subject = random.choice(SUBJECTS)
        style = random.choice(STYLES)
        workflow = random.choice([w for w in WORKFLOWS if "Body" in w or "Face" in w])

        # Basic generation
        pairs.append({
            "instruction": f"generate an image of {subject}",
            "response": tc("run_workflow", name=workflow, positive=subject)
        })

        # With style
        pairs.append({
            "instruction": f"make {subject}, {style}",
            "response": tc("run_workflow", name=workflow, positive=f"{subject}, {style}")
        })

        # Specific workflow
        pairs.append({
            "instruction": f"run the {workflow.replace('.json','')} workflow with {subject}",
            "response": tc("run_workflow", name=workflow, positive=subject)
        })

        # With negative
        pairs.append({
            "instruction": f"generate {subject} but avoid blurry or low quality",
            "response": tc("run_workflow", name=workflow, positive=subject,
                          negative="blurry, low quality, bad anatomy")
        })

        # Multiple
        n = random.choice([2, 3, 4])
        pairs.append({
            "instruction": f"generate {n} variations of {subject}",
            "response": tc("run_workflow", name=workflow, positive=subject, count=n)
        })

        # With checkpoint
        ckpt = random.choice(CHECKPOINTS)
        pairs.append({
            "instruction": f"generate {subject} using {ckpt}",
            "response": tc("run_workflow", name=workflow, positive=subject, checkpoint=ckpt)
        })

    return pairs


def video_generation_pairs():
    pairs = []
    for _ in range(20):
        subject = random.choice(SUBJECTS)
        duration = random.choice([3, 5, 8, 10])

        pairs.append({
            "instruction": f"make a video of {subject}",
            "response": tc("run_workflow", name="Video.json", positive=subject,
                          target="video")
        })
        pairs.append({
            "instruction": f"animate {subject} for {duration} seconds",
            "response": tc("run_workflow", name="Video.json", positive=subject,
                          target="video", duration=duration)
        })
        pairs.append({
            "instruction": "convert this image to video",
            "response": tc("run_workflow", name="Video.json", positive="",
                          target="video", use_image=True)
        })
        n = random.choice([2, 3])
        pairs.append({
            "instruction": f"generate {n} videos of {subject}",
            "response": tc("run_workflow", name="Video.json", positive=subject,
                          target="video", count=n)
        })

    return pairs


def special_workflow_pairs():
    pairs = []
    for _ in range(15):
        pairs.append({
            "instruction": "remove the background from this image",
            "response": tc("run_workflow", name="Remove Background.json")
        })
        pairs.append({
            "instruction": "upscale this image",
            "response": tc("run_workflow", name="Upscale.json")
        })
        pairs.append({
            "instruction": "upscale the last image I generated",
            "response": tc("run_workflow", name="Upscale.json")
        })
        subject = random.choice(SUBJECTS)
        pairs.append({
            "instruction": f"img2img this with the prompt: {subject}",
            "response": tc("run_workflow", name="Img2Img.json", positive=subject)
        })

    return pairs


def lora_training_pairs():
    pairs = []
    for _ in range(20):
        subject = random.choice(LORA_SUBJECTS)
        dataset = random.choice(DATASETS)
        steps = random.choice([500, 1000, 1500, 2000])
        ckpt = random.choice(CHECKPOINTS)

        pairs.append({
            "instruction": f"train a lora of {subject}",
            "response": tc("train_lora", dataset=dataset)
        })
        pairs.append({
            "instruction": f"train a lora with {steps} steps using {ckpt}",
            "response": tc("train_lora", dataset=dataset, steps=steps, checkpoint=ckpt)
        })
        pairs.append({
            "instruction": f"train lora with caption '{subject}'",
            "response": tc("train_lora", dataset=dataset, caption=subject)
        })
        paths = [f"I:\\images\\img{i}.png" for i in range(1, 4)]
        pairs.append({
            "instruction": f"create lora from these images: {', '.join(paths)}",
            "response": tc("train_lora", dataset=dataset, images=paths)
        })

    return pairs


def file_navigation_pairs():
    pairs = []
    for _ in range(25):
        folder = random.choice(FOLDERS)
        subject = random.choice(SUBJECTS).split()[0]

        pairs.append({
            "instruction": f"find {subject}",
            "response": tc("smart_search", query=subject)
        })
        pairs.append({
            "instruction": f"show me {folder}",
            "response": tc("list_directory", path=folder)
        })
        pairs.append({
            "instruction": f"list contents of {folder}",
            "response": tc("list_directory", path=folder)
        })
        pairs.append({
            "instruction": f"explore {folder}",
            "response": tc("explore_tree", path=folder, depth=2)
        })
        pairs.append({
            "instruction": f"search for {subject} in {folder}",
            "response": tc("find_file", pattern=f"*{subject}*", search_root=folder)
        })
        pairs.append({
            "instruction": f"find all png files in {folder}",
            "response": tc("find_file", pattern="*.png", search_root=folder,
                          images_only=True)
        })

    return pairs


def image_browsing_pairs():
    pairs = []
    for _ in range(20):
        folder = random.choice(FOLDERS)
        n = random.choice([4, 8, 12, 24])

        pairs.append({
            "instruction": f"show images from {folder}",
            "response": tc("show_images", folder=folder, count=12)
        })
        pairs.append({
            "instruction": f"display {n} images from {folder}",
            "response": tc("show_images", folder=folder, count=n)
        })
        pairs.append({
            "instruction": "show my favorite images",
            "response": tc("get_favorites")
        })
        pairs.append({
            "instruction": f"pick {n} favorites from {folder}",
            "response": tc("show_images", folder=folder, count=n,
                          random_sample=True)
        })
        pairs.append({
            "instruction": "find images similar to this one",
            "response": tc("visual_search",
                          path="I:\\System 32's Output\\generated images\\last.png",
                          top_n=10, threshold=15)
        })

    return pairs


def memory_pairs():
    pairs = []
    facts = [
        "I prefer 1024x1024 images",
        "my favorite voice is af_heart",
        "the fox lora works best at 0.75 strength",
        "I like anime style generations",
        "preferred steps is 32",
    ]
    for _ in range(20):
        fact = random.choice(facts)
        topic = random.choice(MEMORY_TOPICS)
        key, value = random.choice(PREFERENCES)

        pairs.append({
            "instruction": f"remember {fact}",
            "response": tc("memory_write", content=fact, permanent=True)
        })
        pairs.append({
            "instruction": f"what do you remember about {topic}",
            "response": tc("memory_search", query=topic)
        })
        pairs.append({
            "instruction": f"set preference {key} to {value}",
            "response": tc("set_preference", key=key, value=value)
        })
        pairs.append({
            "instruction": "show my preferences",
            "response": tc("memory_read", tier="operator")
        })
        pairs.append({
            "instruction": "read your permanent memory",
            "response": tc("memory_read", tier="permanent")
        })
        pairs.append({
            "instruction": "write to memory: last session we worked on the USB integration",
            "response": tc("memory_write",
                          content="last session we worked on the USB integration",
                          permanent=False)
        })

    return pairs


def screen_pairs():
    pairs = []
    for _ in range(15):
        pairs.append({
            "instruction": "what's on my screen?",
            "response": tc("see_screen")
        })
        pairs.append({
            "instruction": "take a peek at my screen",
            "response": tc("see_screen")
        })
        pairs.append({
            "instruction": "what am I looking at?",
            "response": tc("see_screen")
        })
        pairs.append({
            "instruction": "can you see what I'm doing?",
            "response": tc("see_screen")
        })

    return pairs


def system_pairs():
    pairs = []
    for _ in range(15):
        pairs.append({
            "instruction": "get system info",
            "response": tc("get_system_info")
        })
        pairs.append({
            "instruction": "show available workflows",
            "response": tc("list_workflows")
        })
        pairs.append({
            "instruction": "check for missing nodes",
            "response": tc("install_missing_nodes")
        })
        pairs.append({
            "instruction": "install missing nodes for image generation",
            "response": tc("install_missing_nodes", target="image")
        })
        pairs.append({
            "instruction": "install missing nodes for video",
            "response": tc("install_missing_nodes", target="video")
        })
        pairs.append({
            "instruction": "build visual index",
            "response": tc("build_visual_index")
        })
        pairs.append({
            "instruction": "rebuild visual index from scratch",
            "response": tc("build_visual_index", force=True)
        })
        pairs.append({
            "instruction": "save current map to index",
            "response": tc("save_map")
        })
        pairs.append({
            "instruction": "what drives are available",
            "response": tc("get_drives")
        })
        pairs.append({
            "instruction": "list my voices",
            "response": tc("list_voices")
        })

    return pairs


def three_d_pairs():
    pairs = []
    for _ in range(10):
        subject = random.choice(SUBJECTS)
        pairs.append({
            "instruction": f"convert this image to 3d",
            "response": tc("run_workflow", name="3D.json", target="3d")
        })
        pairs.append({
            "instruction": f"generate a 3d model of {subject}",
            "response": tc("run_workflow", name="3D.json", positive=subject,
                          target="3d")
        })

    return pairs


# ── Assemble and write ─────────────────────────────────────────────────────────

def main():
    all_pairs = []
    all_pairs.extend(image_generation_pairs())
    all_pairs.extend(video_generation_pairs())
    all_pairs.extend(special_workflow_pairs())
    all_pairs.extend(lora_training_pairs())
    all_pairs.extend(file_navigation_pairs())
    all_pairs.extend(image_browsing_pairs())
    all_pairs.extend(memory_pairs())
    all_pairs.extend(screen_pairs())
    all_pairs.extend(system_pairs())
    all_pairs.extend(three_d_pairs())

    random.shuffle(all_pairs)

    # Write Alpaca format JSONL
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "training_data.jsonl")
    with open(out_path, "w", encoding="utf-8") as f:
        for pair in all_pairs:
            f.write(json.dumps({
                "instruction": pair["instruction"],
                "input": "",
                "output": pair["response"]
            }, ensure_ascii=False) + "\n")

    print(f"Written {len(all_pairs)} training pairs to {out_path}")

    # Also write a human-readable preview of first 10
    print("\nFirst 10 pairs:")
    for i, pair in enumerate(all_pairs[:10]):
        print(f"\n[{i+1}] USER: {pair['instruction']}")
        print(f"     TOOL: {pair['response']}")


if __name__ == "__main__":
    main()
