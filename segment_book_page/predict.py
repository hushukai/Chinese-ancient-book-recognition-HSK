# -*- encoding: utf-8 -*-
# Author: hushukai

from segment_base.predict import segment_predict


def segment_book_page_predict():
    segment_predict(images=None,
                    img_paths="***",
                    dest_dir="***",
                    segment_model=None,
                    segment_task="book_page",
                    text_type="vertical",
                    model_struc="densenet_gru",
                    weights="")


if __name__ == '__main__':
    print("Done !")
