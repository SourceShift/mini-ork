from slugify import slugify


def main():
    assert slugify("Mini Ork") == "mini-ork"
    assert slugify("  Trace Governed Budget  ") == "trace-governed-budget"


if __name__ == "__main__":
    main()
