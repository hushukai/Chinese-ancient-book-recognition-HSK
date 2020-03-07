# -- coding: utf-8 --
# Author: hushukai

import os
import tensorflow as tf
from tensorflow.keras import backend, layers, models, optimizers, callbacks

from detection_yolo3.util import get_anchors, draw_boxes
from detection_yolo3.model import yolo_body, yolo_loss, yolo_eval
from detection_yolo3.data_pipeline import data_generator

from utils import check_or_makedirs
from config import YOLO3_BOOK_PAGE_TAGS_FILE, VALIDATION_SPLIT
from config import BATCH_SIZE_BOOK_PAGE
from config import BOX_CLASSES_ON_BOOK, YOLO3_ANCHORS_FILE
from config import YOLO3_CKPT_DIR, YOLO3_LOGS_DIR
from config import YOLO3_CLASS_SCORE_THRESH, YOLO3_NMS_IOU_THRESH
from config import YOLO3_NMS_MAX_BOXES_NUM


def train(data_file, src_type, model_struc="densenet", pretrained_path="", freeze_body=False):
    check_or_makedirs(YOLO3_LOGS_DIR)
    check_or_makedirs(YOLO3_CKPT_DIR)
    backend.set_learning_phase(True)
    
    num_classes = len(BOX_CLASSES_ON_BOOK)
    anchors = get_anchors(anchors_path=YOLO3_ANCHORS_FILE)

    summary_writer = tf.summary.create_file_writer(YOLO3_LOGS_DIR)
    with summary_writer.as_default():
        load_pretrained = True if os.path.exists(pretrained_path) else False
        model = create_model(anchors, num_classes, model_struc,
                             load_pretrained=load_pretrained,
                             weights_path=pretrained_path,
                             freeze_body=freeze_body)  # make sure you know what you freeze
        summary_writer.flush()
    
    logging = callbacks.TensorBoard(log_dir=YOLO3_LOGS_DIR)
    ckpt_path = os.path.join(YOLO3_CKPT_DIR, "ep{epoch:05d}-loss{loss:.3f}-val_loss{val_loss:.3f}.h5")
    checkpoint = callbacks.ModelCheckpoint(filepath=ckpt_path,
                                           monitor='val_loss',
                                           verbose=1,
                                           save_best_only=True,
                                           save_weights_only=True,  # 只存储weights
                                           mode="min",
                                           period=1)
    reduce_lr = callbacks.ReduceLROnPlateau(monitor='val_loss',     # 当评价指标不再提升时，减小学习率
                                            factor=0.1,
                                            patience=8,
                                            verbose=1)
    early_stopping = callbacks.EarlyStopping(monitor='val_loss',    # 当测试集准确率下降时终止
                                             min_delta=0,
                                             patience=10,
                                             verbose=1,
                                             restore_best_weights=True)

    training_generator, validation_generator = data_generator(data_file=data_file,
                                                              batch_size=BATCH_SIZE_BOOK_PAGE,
                                                              anchors=anchors,
                                                              num_classes=num_classes,
                                                              src_type=src_type)

    # 将目标y_true与input_images一同作为inputs，构建多输入模型，计算loss并将其作为模型输出，
    # 那么，使用compile定义损失的时候，y_pred实际上就是loss，这里直接忽略y_true(即targets)，
    # 训练时,由于y_true作为inputs的一部分，targets随便扔一个符合形状的数组进去即可。
    if freeze_body:   # 冻结式训练
        model.compile(optimizer=optimizers.Adam(lr=1e-3),
                      loss={'yolo_loss': lambda _targets, model_loss: model_loss})  # 使用定制的yolo_loss作为损失
        
        print('Training starts: validation_split {}, batch_size {}.'.format(VALIDATION_SPLIT, BATCH_SIZE_BOOK_PAGE))
        model.fit_generator(generator=training_generator,
                            steps_per_epoch=1000,
                            validation_data=validation_generator,
                            validation_steps=50,
                            epochs=1,
                            initial_epoch=0,
                            callbacks=[logging, checkpoint])
        model.save_weights(os.path.join(YOLO3_CKPT_DIR, "trained_weights_stage_1.h5"))

    if True:  # 整体训练
        # Note that more GPU memory is required after unfreezing the body
        print('Unfreeze all of layers.')
        for i in range(len(model.layers)):
            model.layers[i].trainable = True

        model.compile(optimizer=optimizers.Adam(lr=1e-4),                            # recompile to apply the change
                      loss={'yolo_loss': lambda _targets, model_loss: model_loss})  # 使用定制的yolo_loss作为损失
        
        print('Training starts: validation_split {}, batch_size {}.'.format(VALIDATION_SPLIT, BATCH_SIZE_BOOK_PAGE))
        model.fit_generator(generator=training_generator,
                            steps_per_epoch=1000,
                            validation_data=validation_generator,
                            validation_steps=50,
                            epochs=300,
                            initial_epoch=50,
                            callbacks=[logging, checkpoint, reduce_lr, early_stopping])
        model.save_weights(os.path.join(YOLO3_CKPT_DIR, 'trained_weights_final.h5'))


def create_model(anchors,
                 num_classes,
                 model_struc="densenet",
                 load_pretrained=False,
                 weights_path="",
                 freeze_body=False):
    backend.clear_session()  # Useful to avoid clutter from old models and layers.
    
    image_input = layers.Input(shape=(None, None, 1))  # 图片输入格式
    num_anchors = len(anchors)
    
    # YOLO3有三种尺度的特征图：size/32, size/16, size/8, 分别对应不同粒度的特征
    # 特征图shape(batch, height, width, 当前尺度下的anchor数，类别数+边框4个+置信度1个)
    y_true = [layers.Input(shape=(None, None, num_anchors//3, num_classes+5)) for _ in range(3)]
    
    model_body = yolo_body(image_input, num_anchors//3, num_classes, model_struc)
    print("Create YOLOv3 model with {} anchors and {} classes.".format(num_anchors, num_classes))

    # 处理模型的输出，提取模型的预测结果。注意这里的设定：一个batch只包含一张图片
    model_outs = [y_out[0] for y_out in model_body.outputs]
    boxes, scores, classes = layers.Lambada(yolo_eval, name='yolo_eval',
                                            arguments={'anchors': anchors,
                                                       'num_classes': num_classes,
                                                       'score_thresh': YOLO3_CLASS_SCORE_THRESH,
                                                       'iou_thresh': YOLO3_NMS_IOU_THRESH,
                                                       'max_boxes': YOLO3_NMS_MAX_BOXES_NUM}
                                            )(model_outs)
    img_with_boxes = tf.py_function(func=draw_boxes,
                                    inp=[image_input[0], boxes, scores, classes],
                                    Tout=tf.uint8)
    img_with_boxes = backend.expand_dims(img_with_boxes, axis=0)
    tf.summary.image(name="img_with_boxes", data=img_with_boxes)
    
    # 加载预训练模型
    if load_pretrained:
        model_body.load_weights(weights_path, by_name=True, skip_mismatch=True)  # 加载参数，跳过错误
        print('Load weights {}.'.format(weights_path))
        if freeze_body:
            num = len(model_body.layers) - 52
            for i in range(num):
                model_body.layers[i].trainable = False  # 将模型层的训练关闭
            print('Freeze the first {} layers of total {} layers.'.format(num, len(model_body.layers)))
    
    model_loss = layers.Lambda(yolo_loss,
                               output_shape=(1,),
                               name='yolo_loss',
                               arguments={'anchors': anchors,
                                          'num_classes': num_classes,
                                          'iou_thresh': 0.5}
                               )(model_body.output + y_true)
    model = models.Model(inputs=[model_body.input] + y_true, outputs=model_loss)  # 模型inputs和outputs
    model.summary()
    
    return model


if __name__ == '__main__':
    train(data_file=YOLO3_BOOK_PAGE_TAGS_FILE, src_type="images")
    print("Done !")