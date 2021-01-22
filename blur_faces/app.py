import json
import logging
import os
import urllib

import boto3
import botocore
import cv2
import numpy as np

logger = logging.getLogger()
logger.setLevel(logging.INFO)

rekognition = boto3.client('rekognition')
s3 = boto3.client('s3')

output_bucket = os.environ['OUTPUT_BUCKET']


def anonymize_face_simple(image, factor=3.0):
    """
    Adrian Rosebrock, Blur and anonymize faces with OpenCV and Python, PyImageSearch,
    https://www.pyimagesearch.com/2020/04/06/blur-and-anonymize-faces-with-opencv-and-python/,
    accessed on 6 January 2021

    Anonymizes faces with a Gaussian blur and OpenCV

    Args:
        image (ndarray): The image to be modified
        factor (float): The blurring kernel scale factor. Increasing the factor will increase the amount of blur applied
            to the face (default is 3.0)

    Returns:
        image (ndarray): The modified image
    """

    # automatically determine the size of the blurring kernel based
    # on the spatial dimensions of the input image
    (h, w) = image.shape[:2]
    kW = int(w / factor)
    kH = int(h / factor)

    # ensure the width of the kernel is odd
    if kW % 2 == 0:
        kW -= 1

    # ensure the height of the kernel is odd
    if kH % 2 == 0:
        kH -= 1

    # apply a Gaussian blur to the input image using our computed
    # kernel size
    return cv2.GaussianBlur(image, (kW, kH), 0)


def anonymize_face_pixelate(image, blocks=10):
    """
    Adrian Rosebrock, Blur and anonymize faces with OpenCV and Python, PyImageSearch,
    https://www.pyimagesearch.com/2020/04/06/blur-and-anonymize-faces-with-opencv-and-python/,
    accessed on 6 January 2021

    Creates a pixelated face blur with OpenCV

    Args:
        image (ndarray): The image to be modified
        blocks (int): Number of pixel blocks (default is 10)

    Returns:
        image (ndarray): The modified image
    """

    # divide the input image into NxN blocks
    (h, w) = image.shape[:2]
    xSteps = np.linspace(0, w, blocks + 1, dtype="int")
    ySteps = np.linspace(0, h, blocks + 1, dtype="int")

    # loop over the blocks in both the x and y direction
    for i in range(1, len(ySteps)):
        for j in range(1, len(xSteps)):
            # compute the starting and ending (x, y)-coordinates
            # for the current block
            startX = xSteps[j - 1]
            startY = ySteps[i - 1]
            endX = xSteps[j]
            endY = ySteps[i]

            # extract the ROI using NumPy array slicing, compute the
            # mean of the ROI, and then draw a rectangle with the
            # mean RGB values over the ROI in the original image
            roi = image[startY:endY, startX:endX]
            (B, G, R) = [int(x) for x in cv2.mean(roi)[:3]]
            cv2.rectangle(image, (startX, startY), (endX, endY),
                          (B, G, R), -1)

    # return the pixelated blurred image
    return image


def lambda_handler(event, context):
    successful_records = []
    failed_records = []

    for record in event['Records']:

        # verify event has reference to S3 object
        try:
            # get metadata of file uploaded to Amazon S3
            bucket = record['s3']['bucket']['name']
            key = urllib.parse.unquote_plus(record['s3']['object']['key'])
            size = int(record['s3']['object']['size'])
            filename = key.split('/')[-1]
            local_filename = '/tmp/{}'.format(filename)
        except KeyError:
            error_message = 'Lambda invoked without S3 event data. Event needs to reference a S3 bucket and object key.'
            add_failed(bucket, error_message, failed_records, key)
            continue

        # verify file size is < 15MB
        if size > 15728640:
            error_message = 'Maximum image size stored as an Amazon S3 object is limited to 15 MB for Amazon Rekognition.'
            add_failed(bucket, error_message, failed_records, key)
            continue

        # verify file is JPEG or PNG
        if local_filename.split('.')[-1] not in ['png', 'jpeg', 'jpg']:
            error_message = 'Unsupported file type. Amazon Rekognition Image currently supports the JPEG and PNG image formats.'
            add_failed(bucket, error_message, failed_records, key)
            continue

        # download file locally to /tmp retrieve metadata
        try:
            s3.download_file(bucket, key, local_filename)
        except botocore.exceptions.ClientError:
            error_message = 'Lambda role does not have permission to call GetObject for the input S3 bucket, or object does not exist.'
            add_failed(bucket, error_message, failed_records, key)
            continue

        image = cv2.imread(local_filename)
        image_height, image_width, channels = image.shape

        # use Amazon Rekognition to detect faces in image uploaded to Amazon S3
        try:
            response = rekognition.detect_faces(Image={"S3Object": {"Bucket": bucket, "Name": key}})

        except rekognition.exceptions.AccessDeniedException:

            error_message = 'Lambda role does not have permission to call DetectFaces in Amazon Rekognition.'
            add_failed(bucket, error_message, failed_records, key)
            continue

        except rekognition.exceptions.InvalidS3ObjectException:

            error_message = 'Unable to get object metadata from S3. Check object key, region and/or access permissions for input S3 bucket.'
            add_failed(bucket, error_message, failed_records, key)
            continue

        # loop through faces detected by Amazon Rekognition
        for detected_face in response['FaceDetails']:

            # calcuate bounding box values
            x1 = int(detected_face['BoundingBox']['Left'] * image_width)
            x2 = x1 + int(detected_face['BoundingBox']['Width'] * image_width)
            y1 = int(detected_face['BoundingBox']['Top'] * image_height)
            y2 = y1 + int(detected_face['BoundingBox']['Height'] * image_height)

            # extract the face ROI
            face = image[y1:y2, x1:x2]

            # anonymize/blur faces
            if os.environ['BLUR_TYPE'] == 'pixelate':
                face = anonymize_face_pixelate(face, blocks=10)
            else:
                face = anonymize_face_simple(face, factor=3.0)

            # store the blurred face in the output image
            image[y1:y2, x1:x2] = face

        # overwrite local image file with blurred faces
        cv2.imwrite(local_filename, image)

        # uploaded modified image to Amazon S3 bucket
        try:
            s3.upload_file(local_filename, output_bucket, key)
        except boto3.exceptions.S3UploadFailedError:
            error_message = 'Lambda role does not have permission to call PutObject for the output S3 bucket.'
            add_failed(bucket, error_message, failed_records, key)
            continue

        # clean up /tmp
        if os.path.exists(local_filename):
            os.remove(local_filename)

        successful_records.append({
            "bucket": bucket,
            "key": key
        })

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "cv2_version": cv2.__version__,
                "failed_records": failed_records,
                "successful_records": successful_records
            }
        )
    }


def add_failed(bucket, error_message, failed_records, key):
    failed_records.append({
        "bucket": bucket,
        "key": key,
        "error_message": error_message
    })
