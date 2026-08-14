def main():
    UNIT_CODE_KEYS = ("unitCode","unit_code","UnitCode")
    spec = {
        "port": 1,
        "unitCode": 2
    }
    print(all(key not in spec for key in UNIT_CODE_KEYS))
if __name__ == "__main__":
    main()
